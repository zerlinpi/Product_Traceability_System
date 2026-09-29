/**
 * Procurement and production flow: purchase orders (采购订单), supplier
 * receipts (供应收货), production orders (生产订单), scan-gun stock-in
 * (扫码枪入库), inventory sync (库存同步), Lingxing status and system settings.
 *
 * Note the two different "inbound" concepts:
 * - inbound receipts = supplier parts arriving (pushed to Lingxing);
 * - scan-gun inbound  = finished goods entering stock after a production QR scan.
 */
import type { AnyRecord } from '../types'
import api from '../index'

// Response shapes the operations pages rely on. They extend AnyRecord, so a
// caller may still read any other field the server sends.

/** Which Lingxing write operations have an endpoint configured. */
export interface LingxingEndpointFlags {
  purchaseOrder: boolean
  inboundReceipt: boolean
  inventorySync: boolean
}

/** GET /api/lingxing/status — push availability, never any secret. */
export interface LingxingStatus extends AnyRecord {
  configured: boolean
  writeEndpointsConfigured: boolean
  endpointsConfigured: LingxingEndpointFlags
}

/** GET / PUT /api/settings — credentials are only ever echoed masked. */
export interface SystemSettings extends AnyRecord {
  requireQualityRelease: boolean
  lingxing: LingxingStatus & {
    appId: string
    appSecret: string
    endpoints: { purchaseOrder: string, inboundReceipt: string, inventorySync: string }
  }
}

/** GET /api/inventory-sync — overall run state plus one line per product in stock. */
export interface InventorySyncBoard extends AnyRecord {
  overall: AnyRecord
  items: AnyRecord[]
}

/** POST /api/inventory-sync — the refreshed board plus this run's outcome. */
export interface InventorySyncResult extends InventorySyncBoard {
  syncStatus: string
  syncedAt: string
  itemCount: number
  results: AnyRecord[]
}

/** POST /api/production-orders/batch — per purchase order outcome. */
export interface ProductionOrderBatchResult extends AnyRecord {
  created: AnyRecord[]
  skipped: AnyRecord[]
  failed: { purchaseOrderId: unknown, poNo?: string, message: string }[]
}

export default {
  // ---- purchase orders ----------------------------------------------------
  /** GET /api/purchase-orders */
  purchaseOrders: () => api.get<AnyRecord[]>('/api/purchase-orders'),
  /** GET /api/purchase-orders/:id */
  purchaseOrder: (id: number) => api.get<AnyRecord>(`/api/purchase-orders/${id}`),
  /** POST /api/purchase-orders (ADMIN, OPERATIONS) — idempotent scope `purchase-orders.create`. */
  createPurchaseOrder: (data: AnyRecord) => api.post<AnyRecord>('/api/purchase-orders', data, { idempotent: 'purchase-orders.create' }),
  /** PUT /api/purchase-orders/:id (ADMIN, OPERATIONS) */
  updatePurchaseOrder: (id: number, data: AnyRecord) => api.put<AnyRecord>(`/api/purchase-orders/${id}`, data),
  /** DELETE /api/purchase-orders/:id (ADMIN, OPERATIONS) */
  deletePurchaseOrder: (id: number) => api.delete<AnyRecord>(`/api/purchase-orders/${id}`),
  /** POST /api/purchase-orders/:id/push — 采购单下单 to Lingxing. */
  pushPurchaseOrder: (id: number) => api.post<AnyRecord>(`/api/purchase-orders/${id}/push`, {}),
  /** GET /api/purchase-orders/:id/sync-status */
  purchaseOrderSyncStatus: (id: number) => api.get<AnyRecord>(`/api/purchase-orders/${id}/sync-status`),
  /** GET /api/purchase-orders/:id/factory-progress (ADMIN, OPERATIONS) */
  factoryProgress: (id: number) => api.get<AnyRecord>(`/api/purchase-orders/${id}/factory-progress`),
  /** 48-column Lingxing template, one order. */
  purchaseOrderExportUrl: (id: number) => `/api/purchase-orders/${id}/export`,
  /** 48-column Lingxing template for every order matching the list filters. */
  purchaseOrdersExportUrl: (filters: { syncStatus?: string, supplierId?: number | string, from?: string, to?: string } = {}) => {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== null && value !== '') {
        query.set(key, String(value))
      }
    }
    const text = query.toString()
    return `/api/purchase-orders/export${text ? `?${text}` : ''}`
  },

  // ---- supplier receipts ----------------------------------------------------
  /** GET /api/inbound-receipts */
  inboundReceipts: () => api.get<AnyRecord[]>('/api/inbound-receipts'),
  /** POST /api/inbound-receipts (ADMIN, WAREHOUSE) */
  createInboundReceipt: (data: AnyRecord) => api.post<AnyRecord>('/api/inbound-receipts', data),
  /** POST /api/inbound-receipts/:id/push (ADMIN, OPERATIONS) */
  pushInboundReceipt: (id: number) => api.post<AnyRecord>(`/api/inbound-receipts/${id}/push`, {}),
  /** GET /api/inbound-receipts/:id/sync-status (ADMIN, OPERATIONS) */
  inboundReceiptSyncStatus: (id: number) => api.get<AnyRecord>(`/api/inbound-receipts/${id}/sync-status`),

  // ---- production orders ----------------------------------------------------
  /** GET /api/production-orders (ADMIN, WAREHOUSE) */
  productionOrders: () => api.get<AnyRecord[]>('/api/production-orders'),
  /** GET /api/production-orders/:id (ADMIN, WAREHOUSE) */
  productionOrder: (id: number) => api.get<AnyRecord>(`/api/production-orders/${id}`),
  /**
   * POST /api/production-orders/batch `{ purchaseOrderIds, external }` —
   * idempotent scope `production-orders.batch`. Each order is handled on its
   * own: already generated ones are `skipped`, rejected ones `failed`.
   */
  createProductionOrders: (data: AnyRecord) => api.post<ProductionOrderBatchResult>('/api/production-orders/batch', data, { idempotent: 'production-orders.batch' }),
  productionOrderQrUrl: (id: number) => `/api/production-orders/${id}/qr`,

  // ---- scan-gun stock-in ----------------------------------------------------
  /** POST /api/scan-gun/lookup — resolve a production QR to its order. */
  scanGunLookup: (code: string) => api.post<AnyRecord>('/api/scan-gun/lookup', { code }),
  /** POST /api/scan-gun/inbound — idempotent scope `scan-gun.inbound`. */
  scanGunInbound: (data: { productionOrderId: number, quantity: number }) => api.post<AnyRecord>('/api/scan-gun/inbound', data, { idempotent: 'scan-gun.inbound' }),
  /** GET /api/inbound-scan-records?limit= — latest finished-goods stock-ins. */
  inboundScanRecords: (limit = 50) => api.get<AnyRecord[]>('/api/inbound-scan-records', { params: { limit } }),

  // ---- inventory sync -------------------------------------------------------
  /** GET /api/inventory-sync (ADMIN, OPERATIONS) */
  inventorySync: () => api.get<InventorySyncBoard>('/api/inventory-sync'),
  /**
   * POST /api/inventory-sync (ADMIN, OPERATIONS) — `{}` syncs everything,
   * `{ productModelIds }` a subset. Fails (502) when no product succeeded.
   */
  syncInventory: (data: AnyRecord = {}) => api.post<InventorySyncResult>('/api/inventory-sync', data),

  // ---- Lingxing / settings ------------------------------------------------
  /** GET /api/lingxing/status (ADMIN, OPERATIONS) — which pushes are enabled. */
  lingxingStatus: () => api.get<LingxingStatus>('/api/lingxing/status'),
  /** GET /api/settings (ADMIN) */
  settings: () => api.get<SystemSettings>('/api/settings'),
  /**
   * PUT /api/settings (ADMIN) — only the keys present are changed:
   * `requireQualityRelease`, `appId` + `appSecret` (together), `lingxingEndpoints`.
   * Answers with the same shape as GET.
   */
  updateSettings: (data: AnyRecord) => api.put<SystemSettings>('/api/settings', data),
}
