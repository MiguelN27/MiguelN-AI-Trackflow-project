export type Warehouse = "Monterrey" | "Zaragoza";

export type ProductSummary = {
  id: string;
  name: string;
  sku: string;
  warehouse: Warehouse;
};

export type Product = ProductSummary & { current_stock: number };
export type ProductCreate = Omit<ProductSummary, "id">;
export type OrderDirection = "inbound" | "outbound";
export type OrderCreate = { product_id: string; quantity: number };

type OrderFields = OrderCreate & {
  id: string;
  created_at: string;
  user_uuid: string;
  product: ProductSummary;
};

export type InboundOrder = OrderFields & { direction: "inbound" };
export type OutboundOrder = OrderFields & { direction: "outbound" };
export type InventoryOrder = InboundOrder | OutboundOrder;