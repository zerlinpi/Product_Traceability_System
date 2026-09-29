/**
 * Master data: products (产品), suppliers (供应商), part types (供应部件) and
 * supplier inventory batches (供应批次).
 */
import type { AnyRecord, Pagination } from '../types'
import api from '../index'

export interface ProductAttributeMeta {
  columns: string[]
  derived: string[]
  imageColumns: string[]
}

/** One page of a 二维码生成批次: the batch, its code sets and the pagination. */
export interface ProductCodeBatchPage {
  batch: AnyRecord
  sets: AnyRecord[]
  pagination: Pagination & { totalPages: number }
}

/** Query parameters accepted by GET /api/supplier-inventory-batches. */
export interface InventoryBatchFilters {
  supplierId?: number
  partTypeId?: number
  /** Only active batches with stock left, of active parts and suppliers. */
  available?: boolean
}

export default {
  // ---- products -----------------------------------------------------------
  /**
   * GET /api/products — every role; operations only see their own products.
   * There is no single-product endpoint: detail pages pick from this list.
   */
  products: () => api.get<AnyRecord[]>('/api/products'),
  /** GET /api/product-attribute-columns — the 产品资料 template. */
  productAttributeColumns: () => api.get<ProductAttributeMeta>('/api/product-attribute-columns'),
  /**
   * POST /api/products (ADMIN, OPERATIONS). Without `components` (or with an
   * empty list) a component-less product is created and `modelCode` is honoured.
   */
  createProduct: (data: AnyRecord) => api.post<AnyRecord>('/api/products', data),
  /**
   * PUT /api/products/:id (ADMIN, OPERATIONS — own products only). Attributes
   * are merged over the stored ones (absent columns are kept, `null` or blank
   * clears a column); `components` is only honoured for ADMIN.
   */
  updateProduct: (id: number, data: AnyRecord) => api.put<AnyRecord>(`/api/products/${id}`, data),
  /** DELETE /api/product-models/:id — only products without codes / records. */
  deleteProduct: (id: number) => api.delete<AnyRecord>(`/api/product-models/${id}`),
  /**
   * POST /api/product-images — multipart upload of a 主图 (PNG/JPG/GIF/WEBP
   * ≤ 5 MB, longest side ≤ 10000 px, ≤ 40 MP; SVG is refused).
   */
  uploadProductImage: (file: File) => {
    const body = new FormData()
    body.append('file', file)
    return api.post<{ url: string, filename?: string }>('/api/product-images', body)
  },

  // ---- legacy per-unit code sets (non-treadmill products) -----------------
  /** GET /api/product-code-batches?productModelId= (ADMIN) */
  productCodeBatches: (productModelId: number) => api.get<AnyRecord[]>('/api/product-code-batches', { params: { productModelId } }),
  /** GET /api/product-code-batches/:id?page&pageSize (ADMIN) */
  productCodeBatch: (id: number, page = 1, pageSize = 40) => api.get<ProductCodeBatchPage>(`/api/product-code-batches/${id}`, { params: { page, pageSize } }),
  /** POST /api/products/:id/code-sets (ADMIN) — returns the generated code sets. */
  createCodeSets: (productModelId: number, data: { prefix: string, quantity: number }) => api.post<AnyRecord[]>(`/api/products/${productModelId}/code-sets`, data),
  productQrcodesZipUrl: (productModelId: number) => `/api/products/${productModelId}/qrcodes.zip`,
  productCodeBatchZipUrl: (batchId: number) => `/api/product-code-batches/${batchId}/qrcodes.zip`,
  productCodeSetZipUrl: (codeSetId: number) => `/api/product-code-sets/${codeSetId}/qrcodes.zip`,

  // ---- suppliers ------------------------------------------------------------
  /** GET /api/suppliers */
  suppliers: () => api.get<AnyRecord[]>('/api/suppliers'),
  /** GET /api/suppliers/:id (ADMIN) — supplier, parts, batches, product usage. */
  supplier: (id: number) => api.get<AnyRecord>(`/api/suppliers/${id}`),
  /** POST /api/suppliers (ADMIN) */
  createSupplier: (data: AnyRecord) => api.post<AnyRecord>('/api/suppliers', data),
  /** PUT /api/suppliers/:id (ADMIN) */
  updateSupplier: (id: number, data: AnyRecord) => api.put<AnyRecord>(`/api/suppliers/${id}`, data),

  // ---- part types -----------------------------------------------------------
  /** GET /api/part-types */
  partTypes: () => api.get<AnyRecord[]>('/api/part-types'),
  /** POST /api/part-types (ADMIN) */
  createPartType: (data: AnyRecord) => api.post<AnyRecord>('/api/part-types', data),
  /** PUT /api/part-types/:id (ADMIN) */
  updatePartType: (id: number, data: AnyRecord) => api.put<AnyRecord>(`/api/part-types/${id}`, data),

  // ---- supplier inventory batches ---------------------------------------
  /** GET /api/supplier-inventory-batches (ADMIN) */
  inventoryBatches: (filters: InventoryBatchFilters = {}) => api.get<AnyRecord[]>('/api/supplier-inventory-batches', {
    params: {
      supplierId: filters.supplierId,
      partTypeId: filters.partTypeId,
      available: filters.available ? 1 : undefined,
    },
  }),
  /** POST /api/supplier-inventory-batches (ADMIN) */
  createInventoryBatch: (data: AnyRecord) => api.post<AnyRecord>('/api/supplier-inventory-batches', data),
  /** PUT /api/supplier-inventory-batches/:id (ADMIN) — the received quantity may not drop below the consumed one. */
  updateInventoryBatch: (id: number, data: AnyRecord) => api.put<AnyRecord>(`/api/supplier-inventory-batches/${id}`, data),
  /** GET /api/supplier-inventory-batches/:id/movements (ADMIN) — RECEIPT / ISSUE / ADJUSTMENT, newest first. */
  inventoryMovements: (id: number) => api.get<AnyRecord[]>(`/api/supplier-inventory-batches/${id}/movements`),
  /** GET /api/supplier-inventory-batches/:id/forward-trace (ADMIN) */
  inventoryForwardTrace: (id: number) => api.get<AnyRecord>(`/api/supplier-inventory-batches/${id}/forward-trace`),
}
