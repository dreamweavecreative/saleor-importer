"""Rule-driven canonical category resolution. No industry names in code."""
import json, re, math, difflib, unicodedata
from django.db import connection
from .categories import CategoryRepository, normalize_name

def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
def _nums(s):
    return set(re.findall(r"\d+(?:\.\d+)?", s))

class CategoryResolver:
    def __init__(self, supplier_id):
        self.supplier_id=supplier_id; self.repo=CategoryRepository()
        self.rules=self._load_rules()
        self.stop=set(self.rules.get("stop_words") or [])
        self.syn=self.rules.get("token_synonyms") or {}
        self.aliases=self.rules.get("primary_aliases") or {}
        self.parents=self.rules.get("primary_parents") or {}
        self.size=self.rules.get("secondary_label_patterns") or {}
        self.reuse_t=float(self.rules.get("auto_accept_confidence",0.92))
        self.create_t=float(self.rules.get("auto_create_confidence",0.75))
        self.max_depth=int((self.rules.get("depth_policy") or {}).get("max_depth",3))
        self.min_products=int(self.rules.get("min_category_products",0))
        self.num_guard=bool(self.rules.get("numeric_distinct_guard",True))
        self.prefix_ratio=float(self.rules.get("prefix_reuse_min_ratio",0.75))
        self.word_ext=bool(self.rules.get("distinct_if_word_extension",True))
        self.superset=bool(self.rules.get("distinct_if_token_superset",True))
        self.sec_re=self.rules.get("secondary_reject_regex")
        self.sec_max=int(self.rules.get("secondary_max_tokens",6))
        self.reject_eq=bool(self.rules.get("reject_secondary_equal_to_parent",True))
        self.sep=self.rules.get("path_separator") or " / "
        self.weights={}; self.nodes={}; self.children={}; self._new=0
        self._load_index()

    def _load_rules(self):
        with connection.cursor() as c:
            c.execute("SELECT rule_key,value FROM catalogue.taxonomy_rules")
            out={}
            for k,v in c.fetchall():
                if isinstance(v,str):
                    try: v=json.loads(v)
                    except Exception: pass
                out[k]=v
            return out

    def _load_index(self):
        self.nodes={}; self.children={}
        with connection.cursor() as c:
            c.execute("""SELECT id,parent_id,name,normalized_name FROM catalogue.canonical_categories
                         WHERE status='ACTIVE'""")
            rows=c.fetchall()
        for i,p,n,nn in rows:
            k=str(i); self.nodes[k]={"id":k,"parent_id":str(p) if p else None,"name":n,"nn":nn}
        for k,v in self.nodes.items(): self.children.setdefault(v["parent_id"],[]).append(k)
        for k,v in self.nodes.items(): v["depth"]=self._depth(k); v["path"]=self._path(k)

    def _depth(self,k):
        d=0; cur=k
        while cur: d+=1; cur=self.nodes[cur]["parent_id"]
        return d
    def _path(self,k):
        parts=[]; cur=k
        while cur: parts.append(self.nodes[cur]["nn"]); cur=self.nodes[cur]["parent_id"]
        return self.sep.join(reversed(parts))
    def find_path(self,path):
        parts=[x.strip() for x in re.split(r"[>/|;\\]",str(path)) if x.strip()]
        t=self.sep.join(normalize_name(x) for x in parts)
        for k,v in self.nodes.items():
            if v["path"]==t: return k
        return None
    def ensure_path(self,path,dry_run):
        parts=[x.strip() for x in re.split(r"[>/|;\\]",str(path)) if x.strip()]
        parent=None
        for p in parts:
            k,_,_,_=self._resolve_level(p,parent,dry_run,allow_create=True)
            if k is None: return None
            parent=k
        return parent
    def clean(self,raw):
        s=_strip_accents(str(raw or "")).lower().strip()
        s=re.sub(r"[^\w\s\-&]"," ",s); s=re.sub(r"\s+"," ",s).strip()
        return " ".join(self.syn.get(t,t) for t in s.split() if t not in self.stop).strip()
    def secondary_ok(self,raw,parent_clean):
        r=str(raw or "").strip()
        if not r: return False,"empty"
        if self.sec_re and re.match(self.sec_re,r.upper()): return False,"model_code_like"
        cl=self.clean(r)
        if not cl: return False,"empty_after_clean"
        pat=self.size.get(parent_clean)
        if pat:
            m=re.match(pat["match"],cl)
            if m:
                n=float(m.group(1))
                n=int(math.floor(n)) if pat.get("round")=="floor" else int(round(n))
                cl=pat["template"].format(n=n)
        if len(cl.split())>self.sec_max: return False,"too_many_tokens"
        if self.reject_eq and cl==parent_clean: return False,"equals_parent"
        return True,cl
    def _score(self,a,b):
        if self.num_guard and _nums(a)!=_nums(b): return 0.0,"NUMERIC_DISTINCT"
        short,lng=(a,b) if len(a)<=len(b) else (b,a)
        if short and lng.startswith(short) and lng!=short:
            ratio=len(short)/max(len(lng),1)
            if lng[len(short):len(short)+1]==" ":
                if self.word_ext: return 0.0,"WORD_EXTENSION"
            elif ratio>=self.prefix_ratio: return max(self.reuse_t,0.95),"TRUNCATION"
        ta={t for t in a.split() if any(ch.isalnum() for ch in t)}
        tb={t for t in b.split() if any(ch.isalnum() for ch in t)}
        if self.superset and ta!=tb and (ta<tb or tb<ta): return 0.0,"TOKEN_SUPERSET"
        jac=len(ta&tb)/len(ta|tb) if (ta or tb) else 0.0
        seq=difflib.SequenceMatcher(None,a,b).ratio()
        return 0.6*jac+0.4*seq,"SIMILARITY"
    def _siblings(self,pk): return [self.nodes[k] for k in self.children.get(pk,[])]
    def _resolve_level(self,cleaned,pk,dry_run,allow_create=True):
        nn=normalize_name(cleaned)
        for s in self._siblings(pk):
            if s["nn"]==nn: return s["id"],1.0,"EXACT","REUSE"
        best,bs,btag=None,0.0,None
        for s in self._siblings(pk):
            sc,tag=self._score(nn,s["nn"])
            if sc>bs: best,bs,btag=s,sc,tag
        if best and bs>=self.reuse_t: return best["id"],bs,btag,"REUSE"
        if best and bs>=self.create_t: return None,bs,"AMBIGUOUS","REVIEW"
        if not allow_create: return pk,0.0,"BELOW_MIN_PRODUCTS","DEMOTED"
        if dry_run:
            self._new+=1; k=f"NEW-{self._new}"
            self.nodes[k]={"id":k,"parent_id":pk,"name":cleaned.title(),"nn":nn}
            self.children.setdefault(pk,[]).append(k)
            self.nodes[k]["depth"]=self._depth(k); self.nodes[k]["path"]=self._path(k)
            return k,1.0,"DERIVED","CREATE"
        node=self.repo.create_category(cleaned.title(),parent_id=pk)
        k=str(node["id"] if isinstance(node,dict) else node)
        self._load_index(); return k,1.0,"DERIVED","CREATE"
    def resolve(self,primary,secondary=None,dry_run=True):
        pc=self.clean(primary)
        if not pc: return {"decision":"INVALID","reason":"empty_primary"}
        if pc in self.aliases:
            pid=self.ensure_path(self.aliases[pc],dry_run)
            if pid is None: return {"decision":"CONFIG_ERROR","reason":"alias path"}
            pconf,pm,pd=1.0,"ALIAS","REUSE"
        else:
            pk=self.ensure_path(self.parents[pc],dry_run) if pc in self.parents else None
            if pc in self.parents and pk is None: return {"decision":"CONFIG_ERROR","reason":"parent path"}
            pid,pconf,pm,pd=self._resolve_level(pc,pk,dry_run)
            if pd=="REVIEW": return {"decision":"REVIEW","level":1,"raw":primary,"confidence":pconf}
        leaf,conf,method=pid,pconf,pm; sec=None
        if secondary and self.nodes[leaf]["depth"]<self.max_depth:
            ok,info=self.secondary_ok(secondary,pc)
            if ok:
                w=self.weights.get((pc,info))
                allow=(w is None or w>=self.min_products)
                sid,sc,sm,sd=self._resolve_level(info,leaf,dry_run,allow_create=allow)
                sec={"raw":secondary,"label":info,"decision":sd,"confidence":sc,"method":sm,"weight":w}
                if sd=="REVIEW": return {"decision":"REVIEW","level":2,"raw":f"{primary}>{secondary}","confidence":sc}
                if sd!="DEMOTED": leaf,conf,method=sid,sc,sm
            else: sec={"raw":secondary,"decision":"REJECTED","reason":info}
        elif secondary: sec={"raw":secondary,"decision":"DEFERRED","reason":"depth_cap"}
        return {"decision":"RESOLVED","canonical_category_id":leaf,"path":self.nodes[leaf]["path"],
                "depth":self.nodes[leaf]["depth"],"confidence":conf,"method":method,"secondary":sec}
