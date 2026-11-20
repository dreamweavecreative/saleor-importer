# ... (keep the top part of your existing normalizers.py the same) ...

        # Robust weight parsing
        weight_kg = None
        try:
            weight_str = str(row.get('weight_kg', '')).replace(',', '').strip()
            if weight_str and weight_str.lower() not in ('', '0', 'n/a', 'none'):
                weight_kg = float(weight_str)
        except (ValueError, TypeError):
            pass # Weight is optional, don't fail the row

        return NormalizedRow(
            import_flag=import_flag,
            supplier=supplier,
            supplier_sku=supplier_sku,
            sku=sku,
            product_name=product_name,
            brand=brand,
            category=category,
            collection=str(row.get('collection', '')).strip(),
            description=str(row.get('description', product_name)).strip(), # Fallback to name if desc is empty
            cost_ex_gst=cost_ex_gst,
            selling_price_inc_gst=selling_price_inc_gst,
            supplier_quantity=supplier_quantity,
            image_url=str(row.get('image_url', '')).strip(),
            weight_kg=weight_kg,
            supplier_url=str(row.get('supplier_url', '')).strip(),
            raw_errors=[]
        )
