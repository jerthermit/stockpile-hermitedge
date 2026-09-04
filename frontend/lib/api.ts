import type {
  AddInvestigationEvidenceRequest,
  AIReviewRecord,
  AIReviewResponse,
  AIReviewType,
  ApproveStockCountRequest,
  CancelStockCountRequest,
  CloseReservationRequest,
  CreateDamageRequest,
  CreateDeliveryRequest,
  CreateInvestigationRequest,
  CreateReservationRequest,
  CreateReturnRequest,
  CreateStockCountRequest,
  CreateTransferRequest,
  CreateProductRequest,
  DamageCase,
  DamageResponse,
  DamageStatus,
  DeliveryDocumentAIReview,
  DeliveryResponse,
  DiscrepancyAIReview,
  ExchangeEvent,
  InvestigationKind,
  InvestigationResponse,
  InvestigationStatus,
  InventoryItem,
  InventoryInvestigation,
  InventoryDelivery,
  InventoryReservation,
  InventoryReturn,
  InventoryStockCount,
  InventoryTransfer,
  InventoryTransaction,
  LoginResponse,
  Movement,
  OrderComparisonBatch,
  OrderComparisonResponse,
  ReceiveTransferRequest,
  ReservationResponse,
  ReservationStatus,
  ResolveDamageRequest,
  ResolveInvestigationRequest,
  ResolveOrderComparisonRequest,
  ReturnResponse,
  StockLocation,
  StockCountResponse,
  StockCountStatus,
  TransferResponse,
  TransferStatus,
  TransactionResponse,
  UpdateProductRequest,
  User,
} from "./types";

const configuredBase =
  process.env.NEXT_PUBLIC_API_URL?.trim() ||
  "http://localhost:8000/api/v1";

export const API_BASE_URL =
  configuredBase.replace(/\/+$/, "");

const MAX_DELIVERY_DOCUMENT_BYTES =
  5_000_000;

export interface InventoryImportResult {
  created: number;
  updated: number;
  skipped: number;
  errors: string[];
}

type WithoutIdempotency<T> =
  T extends unknown
    ? Omit<T, "idempotency_key">
    : never;

export class ApiError extends Error {
  status: number;
  code: string | null;
  retryable: boolean;

  constructor(
    message: string,
    status = 0,
    code: string | null = null,
    retryable = false,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}

interface ApiFailureMetadata {
  code: string | null;
  retryable: boolean | null;
}

function unwrap<T>(
  payload: T | { data: T },
): T {
  if (
    payload &&
    typeof payload === "object" &&
    "data" in payload
  ) {
    return (payload as { data: T }).data;
  }

  return payload as T;
}

function listParameters(
  limit: number,
  status?: string,
): string {
  const search = new URLSearchParams({
    limit: String(limit),
  });

  if (status) {
    search.set("status", status);
  }

  return search.toString();
}

function errorMessage(
  payload: unknown,
  fallback: string,
): string {
  if (
    !payload ||
    typeof payload !== "object"
  ) {
    return fallback;
  }

  const detail = (
    payload as {
      detail?: unknown;
      message?: unknown;
    }
  ).detail;

  const message = (
    payload as {
      message?: unknown;
    }
  ).message;

  if (typeof detail === "string") {
    return detail;
  }

  if (typeof message === "string") {
    return message;
  }

  if (
    detail &&
    typeof detail === "object" &&
    "message" in detail
  ) {
    return String(
      (
        detail as {
          message: unknown;
        }
      ).message,
    );
  }

  if (Array.isArray(detail)) {
    return detail
      .map((entry) => {
        if (
          entry &&
          typeof entry === "object" &&
          "msg" in entry
        ) {
          return String(
            (
              entry as {
                msg: unknown;
              }
            ).msg,
          );
        }

        return String(entry);
      })
      .join(". ");
  }

  return fallback;
}

function errorMetadata(
  payload: unknown,
): ApiFailureMetadata {
  if (
    !payload ||
    typeof payload !== "object"
  ) {
    return {
      code: null,
      retryable: null,
    };
  }

  const detail = (
    payload as {
      detail?: unknown;
    }
  ).detail;

  if (
    !detail ||
    typeof detail !== "object" ||
    Array.isArray(detail)
  ) {
    return {
      code: null,
      retryable: null,
    };
  }

  const rawCode = (
    detail as {
      code?: unknown;
    }
  ).code;
  const rawRetryable = (
    detail as {
      retryable?: unknown;
    }
  ).retryable;

  return {
    code:
      typeof rawCode === "string"
        ? rawCode
        : null,
    retryable:
      typeof rawRetryable === "boolean"
        ? rawRetryable
        : null,
  };
}

function safeHeaderFilename(
  filename: string,
): string {
  const normalized = filename
    .normalize("NFKD")
    .replace(/[^\x20-\x7E]/g, "_")
    .replace(/[\r\n]/g, "_")
    .trim();

  return (
    normalized.slice(0, 180) ||
    "delivery-document"
  );
}

function aiRequestKey(
  key: string,
): string {
  const normalized = key.trim();
  if (
    normalized.length < 8 ||
    normalized.length > 128
  ) {
    throw new ApiError(
      "The review request key is invalid.",
      422,
      "invalid_idempotency_key",
    );
  }
  return normalized;
}

async function request<T>(
  path: string,
  options: RequestInit = {},
  token?: string,
): Promise<T> {
  const headers = new Headers(
    options.headers,
  );

  if (!headers.has("Accept")) {
    headers.set(
      "Accept",
      "application/json",
    );
  }

  if (
    typeof options.body === "string" &&
    !headers.has("Content-Type")
  ) {
    headers.set(
      "Content-Type",
      "application/json",
    );
  }

  if (token) {
    headers.set(
      "Authorization",
      `Bearer ${token}`,
    );
  }

  let response: Response;

  try {
    response = await fetch(
      `${API_BASE_URL}${path}`,
      {
        ...options,
        headers,
        cache: "no-store",
      },
    );
  } catch {
    throw new ApiError(
      "Cannot reach Stockpile. Check that the API is running, then retry.",
      0,
      "network_error",
      true,
    );
  }

  const payload = await response
    .json()
    .catch(() => null);

  if (!response.ok) {
    const metadata = errorMetadata(
      payload,
    );
    throw new ApiError(
      errorMessage(
        payload,
        `Request failed (${response.status})`,
      ),
      response.status,
      metadata.code,
      metadata.retryable ??
        (
          response.status === 429 ||
          response.status >= 500
        ),
    );
  }

  return payload as T;
}

function authenticatedResourceUrl(
  path: string,
): string {
  const fallbackOrigin =
    typeof window === "undefined"
      ? "http://localhost"
      : window.location.origin;

  let apiBase: URL;
  let resource: URL;
  try {
    apiBase = new URL(
      API_BASE_URL,
      fallbackOrigin,
    );
    resource = new URL(
      path,
      apiBase.origin,
    );
  } catch {
    throw new ApiError(
      "The product image address is invalid.",
    );
  }

  if (resource.origin !== apiBase.origin) {
    throw new ApiError(
      "The product image address is invalid.",
    );
  }

  return resource.toString();
}

async function requestImage(
  path: string,
  token: string,
): Promise<Blob> {
  let response: Response;
  try {
    response = await fetch(
      authenticatedResourceUrl(path),
      {
        headers: {
          Accept: "image/webp,image/*",
          Authorization: `Bearer ${token}`,
        },
        cache: "force-cache",
      },
    );
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    throw new ApiError(
      "The product image could not be loaded.",
      0,
      "network_error",
      true,
    );
  }

  if (!response.ok) {
    const payload = await response
      .json()
      .catch(() => null);
    const metadata = errorMetadata(
      payload,
    );
    throw new ApiError(
      errorMessage(
        payload,
        `Image request failed (${response.status})`,
      ),
      response.status,
      metadata.code,
      metadata.retryable ??
        response.status >= 500,
    );
  }

  const contentType =
    response.headers.get("Content-Type") || "";
  if (!contentType.startsWith("image/")) {
    throw new ApiError(
      "The product image response was invalid.",
    );
  }

  return response.blob();
}

export const stockpileApi = {
  login(
    username: string,
    password: string,
  ) {
    return request<LoginResponse>(
      "/auth/login",
      {
        method: "POST",
        body: JSON.stringify({
          username,
          password,
        }),
      },
    );
  },

  me(token: string) {
    return request<{ user: User }>(
      "/auth/me",
      {},
      token,
    );
  },

  async inventory(token: string) {
    const response = await request<
      | InventoryItem[]
      | { data: InventoryItem[] }
    >("/inventory", {}, token);

    return unwrap(response);
  },

  async barcode(
    token: string,
    barcode: string,
  ) {
    const response = await request<
      | InventoryItem
      | { data: InventoryItem }
    >(
      `/inventory/barcodes/${encodeURIComponent(
        barcode,
      )}`,
      {},
      token,
    );

    return unwrap(response);
  },

  async locations(token: string) {
    const response = await request<
      | StockLocation[]
      | { data: StockLocation[] }
    >(
      "/inventory/locations",
      {},
      token,
    );

    return unwrap(response);
  },

  async transactions(token: string) {
    const response = await request<
      | InventoryTransaction[]
      | {
          data: InventoryTransaction[];
        }
    >(
      "/inventory/transactions",
      {},
      token,
    );

    return unwrap(response);
  },

  createTransaction(
    token: string,
    key: string,
    input: {
      barcode: string;
      movement_type: Movement;
      quantity: number;
      location_code: string;
      reason?: string;
      source: "scanner" | "manual";
    },
  ) {
    return request<TransactionResponse>(
      "/inventory/transactions",
      {
        method: "POST",
        body: JSON.stringify({
          ...input,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  reverseTransaction(
    token: string,
    id: string,
    key: string,
    reason: string,
  ) {
    return request<TransactionResponse>(
      `/inventory/transactions/${encodeURIComponent(
        id,
      )}/reverse`,
      {
        method: "POST",
        body: JSON.stringify({
          reason,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  async transfers(
    token: string,
    transferStatus?: TransferStatus,
    limit = 100,
  ) {
    const search = new URLSearchParams({
      limit: String(limit),
    });

    if (transferStatus) {
      search.set(
        "status",
        transferStatus,
      );
    }

    const response = await request<
      | InventoryTransfer[]
      | { data: InventoryTransfer[] }
    >(
      `/inventory/transfers?${search.toString()}`,
      {},
      token,
    );

    return unwrap(response);
  },

  createTransfer(
    token: string,
    key: string,
    input: Omit<
      CreateTransferRequest,
      "idempotency_key"
    >,
  ) {
    return request<TransferResponse>(
      "/inventory/transfers",
      {
        method: "POST",
        body: JSON.stringify({
          ...input,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  receiveTransfer(
    token: string,
    transferId: string,
    key: string,
    input: Omit<
      ReceiveTransferRequest,
      "idempotency_key"
    >,
  ) {
    return request<TransferResponse>(
      `/inventory/transfers/${encodeURIComponent(
        transferId,
      )}/receive`,
      {
        method: "POST",
        body: JSON.stringify({
          ...input,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  async deliveries(
    token: string,
    limit = 100,
  ) {
    const response = await request<
      | InventoryDelivery[]
      | { data: InventoryDelivery[] }
    >(
      `/inventory/deliveries?${listParameters(
        limit,
      )}`,
      {},
      token,
    );

    return unwrap(response);
  },

  createDelivery(
    token: string,
    key: string,
    input: Omit<
      CreateDeliveryRequest,
      "idempotency_key"
    >,
  ) {
    return request<DeliveryResponse>(
      "/inventory/deliveries",
      {
        method: "POST",
        body: JSON.stringify({
          ...input,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  async returns(
    token: string,
    limit = 100,
  ) {
    const response = await request<
      | InventoryReturn[]
      | { data: InventoryReturn[] }
    >(
      `/inventory/returns?${listParameters(
        limit,
      )}`,
      {},
      token,
    );

    return unwrap(response);
  },

  createReturn(
    token: string,
    key: string,
    input: Omit<
      CreateReturnRequest,
      "idempotency_key"
    >,
  ) {
    return request<ReturnResponse>(
      "/inventory/returns",
      {
        method: "POST",
        body: JSON.stringify({
          ...input,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  async damageCases(
    token: string,
    damageStatus?: DamageStatus,
    limit = 200,
  ) {
    const response = await request<
      | DamageCase[]
      | { data: DamageCase[] }
    >(
      `/inventory/damage?${listParameters(
        limit,
        damageStatus,
      )}`,
      {},
      token,
    );

    return unwrap(response);
  },

  createDamage(
    token: string,
    key: string,
    input: Omit<
      CreateDamageRequest,
      "idempotency_key"
    >,
  ) {
    return request<DamageResponse>(
      "/inventory/damage",
      {
        method: "POST",
        body: JSON.stringify({
          ...input,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  resolveDamage(
    token: string,
    damageId: string,
    key: string,
    input: Omit<
      ResolveDamageRequest,
      "idempotency_key"
    >,
  ) {
    return request<DamageResponse>(
      `/inventory/damage/${encodeURIComponent(
        damageId,
      )}/resolve`,
      {
        method: "POST",
        body: JSON.stringify({
          ...input,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  async investigations(
    token: string,
    filters: {
      status?: InvestigationStatus;
      kind?: InvestigationKind;
      locationCode?: string;
    } = {},
    limit = 200,
  ) {
    const search = new URLSearchParams({
      limit: String(limit),
    });

    if (filters.status) {
      search.set("status", filters.status);
    }

    if (filters.kind) {
      search.set("kind", filters.kind);
    }

    if (filters.locationCode?.trim()) {
      search.set(
        "location_code",
        filters.locationCode.trim(),
      );
    }

    const response = await request<
      | InventoryInvestigation[]
      | { data: InventoryInvestigation[] }
    >(
      `/inventory/investigations?${search.toString()}`,
      {},
      token,
    );

    return unwrap(response);
  },

  createInvestigation(
    token: string,
    key: string,
    input: Omit<
      CreateInvestigationRequest,
      "idempotency_key"
    >,
  ) {
    return request<InvestigationResponse>(
      "/inventory/investigations",
      {
        method: "POST",
        body: JSON.stringify({
          ...input,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  addInvestigationEvidence(
    token: string,
    investigationId: string,
    key: string,
    input: Omit<
      AddInvestigationEvidenceRequest,
      "idempotency_key"
    >,
  ) {
    return request<InvestigationResponse>(
      `/inventory/investigations/${encodeURIComponent(
        investigationId,
      )}/evidence`,
      {
        method: "POST",
        body: JSON.stringify({
          ...input,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  resolveInvestigation(
    token: string,
    investigationId: string,
    key: string,
    input: Omit<
      ResolveInvestigationRequest,
      "idempotency_key"
    >,
  ) {
    return request<InvestigationResponse>(
      `/inventory/investigations/${encodeURIComponent(
        investigationId,
      )}/resolve`,
      {
        method: "POST",
        body: JSON.stringify({
          ...input,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  async reservations(
    token: string,
    reservationStatus?: ReservationStatus,
    limit = 200,
  ) {
    const response = await request<
      | InventoryReservation[]
      | { data: InventoryReservation[] }
    >(
      `/inventory/reservations?${listParameters(
        limit,
        reservationStatus,
      )}`,
      {},
      token,
    );

    return unwrap(response);
  },

  createReservation(
    token: string,
    key: string,
    input: Omit<
      CreateReservationRequest,
      "idempotency_key"
    >,
  ) {
    return request<ReservationResponse>(
      "/inventory/reservations",
      {
        method: "POST",
        body: JSON.stringify({
          ...input,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  closeReservation(
    token: string,
    reservationId: string,
    key: string,
    input: Omit<
      CloseReservationRequest,
      "idempotency_key"
    >,
  ) {
    return request<ReservationResponse>(
      `/inventory/reservations/${encodeURIComponent(
        reservationId,
      )}/close`,
      {
        method: "POST",
        body: JSON.stringify({
          ...input,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  async orderComparisons(
    token: string,
    limit = 50,
  ) {
    const response = await request<
      | OrderComparisonBatch[]
      | { data: OrderComparisonBatch[] }
    >(
      `/inventory/orders/comparisons?${listParameters(
        limit,
      )}`,
      {},
      token,
    );

    return unwrap(response);
  },

  importSellingOrders(
    token: string,
    key: string,
    salesChannel: string,
    file: File,
  ) {
    const form = new FormData();

    form.append("file", file);
    form.append(
      "sales_channel",
      salesChannel,
    );
    form.append("idempotency_key", key);

    return request<OrderComparisonResponse>(
      "/inventory/orders/comparisons/import",
      {
        method: "POST",
        body: form,
      },
      token,
    );
  },

  resolveOrderComparison(
    token: string,
    lineId: string,
    key: string,
    input: WithoutIdempotency<
      ResolveOrderComparisonRequest
    >,
  ) {
    return request<OrderComparisonResponse>(
      `/inventory/orders/comparisons/lines/${encodeURIComponent(
        lineId,
      )}/resolve`,
      {
        method: "POST",
        body: JSON.stringify({
          ...input,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  async aiReviews(
    token: string,
    reviewType?: AIReviewType,
    limit = 50,
  ) {
    const search = new URLSearchParams({
      limit: String(limit),
    });
    if (reviewType) {
      search.set(
        "review_type",
        reviewType,
      );
    }

    const response = await request<
      | AIReviewRecord[]
      | { data: AIReviewRecord[] }
    >(
      `/inventory/ai/reviews?${search.toString()}`,
      {},
      token,
    );

    return unwrap(response);
  },

  reviewDeliveryDocument(
    token: string,
    key: string,
    file: File,
  ) {
    if (file.size === 0) {
      throw new ApiError(
        "Choose a delivery document image.",
        422,
        "empty_document",
      );
    }
    if (
      file.size >
      MAX_DELIVERY_DOCUMENT_BYTES
    ) {
      throw new ApiError(
        "Delivery document images must be 5 MB or smaller.",
        413,
        "document_too_large",
      );
    }

    return request<
      AIReviewResponse<DeliveryDocumentAIReview>
    >(
      "/inventory/ai/delivery-document",
      {
        method: "POST",
        headers: {
          "Content-Type":
            file.type ||
            "application/octet-stream",
          "Idempotency-Key":
            aiRequestKey(key),
          "X-Stockpile-Filename":
            safeHeaderFilename(file.name),
        },
        body: file,
      },
      token,
    );
  },

  reviewDiscrepancies(
    token: string,
    key: string,
  ) {
    return request<
      AIReviewResponse<DiscrepancyAIReview>
    >(
      "/inventory/ai/discrepancies",
      {
        method: "POST",
        headers: {
          "Idempotency-Key":
            aiRequestKey(key),
        },
      },
      token,
    );
  },

  async stockCounts(
    token: string,
    countStatus?: StockCountStatus,
    limit = 100,
  ) {
    const response = await request<
      | InventoryStockCount[]
      | { data: InventoryStockCount[] }
    >(
      `/inventory/counts?${listParameters(
        limit,
        countStatus,
      )}`,
      {},
      token,
    );

    return unwrap(response);
  },

  createStockCount(
    token: string,
    key: string,
    input: Omit<
      CreateStockCountRequest,
      "idempotency_key"
    >,
  ) {
    return request<StockCountResponse>(
      "/inventory/counts",
      {
        method: "POST",
        body: JSON.stringify({
          ...input,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  approveStockCount(
    token: string,
    countId: string,
    key: string,
    input: Omit<
      ApproveStockCountRequest,
      "idempotency_key"
    >,
  ) {
    return request<StockCountResponse>(
      `/inventory/counts/${encodeURIComponent(
        countId,
      )}/approve`,
      {
        method: "POST",
        body: JSON.stringify({
          ...input,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  cancelStockCount(
    token: string,
    countId: string,
    key: string,
    input: Omit<
      CancelStockCountRequest,
      "idempotency_key"
    >,
  ) {
    return request<StockCountResponse>(
      `/inventory/counts/${encodeURIComponent(
        countId,
      )}/cancel`,
      {
        method: "POST",
        body: JSON.stringify({
          ...input,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  createProduct(
    token: string,
    key: string,
    input: CreateProductRequest,
  ) {
    return request<TransactionResponse>(
      "/inventory/products",
      {
        method: "POST",
        body: JSON.stringify({
          ...input,
          idempotency_key: key,
        }),
      },
      token,
    );
  },

  async updateProduct(
    token: string,
    sku: string,
    input: UpdateProductRequest,
  ) {
    const response = await request<
      | InventoryItem
      | { data: InventoryItem }
    >(
      `/inventory/products/${encodeURIComponent(
        sku,
      )}`,
      {
        method: "PATCH",
        body: JSON.stringify(input),
      },
      token,
    );

    return unwrap(response);
  },

  async uploadProductImage(
    token: string,
    sku: string,
    file: File,
  ) {
    const form = new FormData();
    form.append("file", file);

    const response = await request<
      | InventoryItem
      | { data: InventoryItem }
    >(
      `/inventory/products/${encodeURIComponent(
        sku,
      )}/image`,
      {
        method: "PUT",
        body: form,
      },
      token,
    );

    return unwrap(response);
  },

  async deleteProductImage(
    token: string,
    sku: string,
  ) {
    const response = await request<
      | InventoryItem
      | { data: InventoryItem }
    >(
      `/inventory/products/${encodeURIComponent(
        sku,
      )}/image`,
      {
        method: "DELETE",
      },
      token,
    );

    return unwrap(response);
  },

  productImage(
    token: string,
    imageUrl: string,
  ) {
    return requestImage(
      imageUrl,
      token,
    );
  },

  async archiveProduct(
    token: string,
    sku: string,
  ) {
    const response = await request<
      | InventoryItem
      | { data: InventoryItem }
    >(
      `/inventory/products/${encodeURIComponent(
        sku,
      )}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          active: false,
        }),
      },
      token,
    );

    return unwrap(response);
  },

  async importInventory(
    token: string,
    key: string,
    file: File,
  ) {
    const form = new FormData();

    form.append("file", file);
    form.append("idempotency_key", key);

    const response = await request<
      | InventoryImportResult
      | { data: InventoryImportResult }
    >(
      "/inventory/import",
      {
        method: "POST",
        body: form,
      },
      token,
    );

    return unwrap(response);
  },

  async exchange(token: string) {
    const response = await request<
      | ExchangeEvent[]
      | { data: ExchangeEvent[] }
    >(
      "/inventory/exchange",
      {},
      token,
    );

    return unwrap(response);
  },

  async retryExchange(
    token: string,
    transactionId?: string,
  ) {
    const response = await request<
      | ExchangeEvent[]
      | { data: ExchangeEvent[] }
    >(
      "/inventory/exchange/retry",
      {
        method: "POST",
        body: JSON.stringify(
          transactionId
            ? {
                transaction_id:
                  transactionId,
              }
            : {},
        ),
      },
      token,
    );

    return unwrap(response);
  },
};
