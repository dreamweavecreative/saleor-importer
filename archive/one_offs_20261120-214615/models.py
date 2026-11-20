from django.db import models
from django.utils import timezone

class SupplierOffer(models.Model):
    """
    Tracks independent supplier offers for a canonical Saleor ProductVariant.
    Allows the purchasing engine to select the best eligible supplier without
    polluting the storefront with duplicate variants.
    """
    variant = models.ForeignKey(
        'product.ProductVariant', 
        on_delete=models.CASCADE, 
        related_name='supplier_offers'
    )
    supplier_code = models.CharField(max_length=50, db_index=True, help_text="e.g., 'DICKER'")
    supplier_sku = models.CharField(max_length=255, db_index=True)
    
    # Offer specifics
    cost_price = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    inventory_quantity = models.IntegerField(default=0)
    lead_time_days = models.IntegerField(default=0, help_text="Days to deliver")
    is_active = models.BooleanField(default=True)
    
    # Audit
    last_synced_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'importer'
        unique_together = ('variant', 'supplier_code')
        indexes = [
            models.Index(fields=['supplier_code', 'is_active']),
            models.Index(fields=['variant', 'cost_price']),
        ]

    def __str__(self):
        return f"{self.supplier_code} - {self.supplier_sku} (Variant: {self.variant_id})"
