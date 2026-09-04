export type Role = "operator" | "admin";

export interface User {
  id: string;
  username: string;
  display_name: string;
  role: Role;
}

export type LocationKind =
  | "warehouse"
  | "zone"
  | "rack"
  | "bin"
  | "receiving"
  | "staging"
  | "dispatch"
  | "holding";

export interface StockLocation {
  code: string;
  name: string;
  kind: LocationKind;
  parent_code: string | null;
  path: string;
  active: boolean;
  stockable: boolean;
}

export interface InventoryPosition {
  location_code: string;
  location_name: string;
  location_path: string;
  quantity: number;
  reserved_quantity: number;
  damaged_quantity: number;
  available_quantity: number;
  last_updated: string;
  version?: number;
}

export interface InventoryItem {
  sku: string;
  barcode: string;
  product_name: string;
  category: string;
  unit?: string;
  variant?: string | null;
  image_url?: string | null;
  acquisition_cost_centavos?: number | null;
  track_individually?: boolean;
  current_stock: number;
  reserved_stock: number;
  damaged_stock: number;
  available_stock: number;
  positions: InventoryPosition[];
  status: string;
  last_updated: string;
  active?: boolean;
  version?: number;
}

export interface CreateProductRequest {
  sku: string;
  barcode: string;
  product_name: string;
  category: string;
  unit: string;
  variant?: string | null;
  acquisition_cost_centavos?: number | null;
  location_code: string;
  quantity: number;
  reason?: string;
}

export interface UpdateProductRequest {
  barcode?: string;
  product_name?: string;
  category?: string;
  unit?: string;
  variant?: string | null;
  acquisition_cost_centavos?: number | null;
  active?: boolean;
}

export type Movement =
  | "stock_in"
  | "stock_out";

export type TransactionSource =
  | "scanner"
  | "manual"
  | "import"
  | "delivery"
  | "return"
  | "damage"
  | "reservation"
  | "count";

export interface InventoryTransaction {
  id: string;
  sku: string;
  barcode: string;
  product_name: string;
  movement_type: Movement | "reversal";
  quantity: number;
  quantity_delta: number;
  balance_before: number;
  balance_after: number;
  actor_user_id: string;
  actor_name: string;
  reason: string | null;
  source: TransactionSource;
  location_code: string;
  location_name: string;
  location_path: string;
  location_balance_before: number;
  location_balance_after: number;
  created_at: string;
  reverses_transaction_id: string | null;
  reversed_by_transaction_id: string | null;
  exchange_status?: ExchangeStatus;
}

export type TransferStatus =
  | "pending"
  | "received"
  | "received_with_discrepancy"
  | "cancelled";

export interface TransferLine {
  sku: string;
  barcode: string;
  product_name: string;
  quantity_sent: number;
  quantity_received: number | null;
  discrepancy_note: string | null;
  source_balance_before: number | null;
  source_balance_after: number | null;
  destination_balance_before: number | null;
  destination_balance_after: number | null;
}

export interface InventoryTransfer {
  id: string;
  status: TransferStatus;
  source_location_code: string;
  source_location_name: string;
  source_location_path: string;
  destination_location_code: string;
  destination_location_name: string;
  destination_location_path: string;
  initiated_by_user_id: string;
  initiated_by_name: string;
  received_by_user_id: string | null;
  received_by_name: string | null;
  note: string | null;
  receive_note: string | null;
  created_at: string;
  received_at: string | null;
  has_discrepancy: boolean;
  lines: TransferLine[];
}

export interface CreateTransferRequest {
  source_location_code: string;
  destination_location_code: string;
  lines: Array<{
    barcode: string;
    quantity: number;
  }>;
  idempotency_key: string;
  note?: string;
}

export interface TransferDiscrepancyRequest {
  barcode: string;
  quantity_received: number;
  note?: string;
}

export interface ReceiveTransferRequest {
  receiver_badge_code: string;
  receiver_pin?: string;
  idempotency_key: string;
  discrepancies?: TransferDiscrepancyRequest[];
  note?: string;
}

export interface TransferResponse {
  data: InventoryTransfer;
  replayed: boolean;
}

export type ReceiptSource =
  | "scanner"
  | "document"
  | "manual"
  | "import";

export interface DeliveryLine {
  sku: string;
  barcode: string;
  product_name: string;
  location_code: string;
  location_name: string;
  location_path: string;
  quantity: number;
  inventory_balance_before: number;
  inventory_balance_after: number;
  location_balance_before: number;
  location_balance_after: number;
  transaction_id: string;
}

export interface InventoryDelivery {
  id: string;
  status: "received";
  supplier_name: string | null;
  reference: string | null;
  source: ReceiptSource;
  receiver_user_id: string;
  receiver_name: string;
  created_by_user_id: string;
  created_by_name: string;
  note: string | null;
  created_at: string;
  received_at: string;
  lines: DeliveryLine[];
}

export interface CreateDeliveryRequest {
  supplier_name?: string;
  reference?: string;
  lines: Array<{
    barcode: string;
    quantity: number;
    location_code: string;
  }>;
  idempotency_key: string;
  source?: ReceiptSource;
  receiver_badge_code: string;
  receiver_pin?: string;
  note?: string;
}

export type ReturnType =
  | "customer_return"
  | "supplier_return";

export type StockCondition =
  | "sellable"
  | "damaged";

export interface InventoryReturnLine {
  sku: string;
  barcode: string;
  product_name: string;
  location_code: string;
  location_name: string;
  location_path: string;
  condition: StockCondition;
  quantity: number;
  inventory_balance_before: number;
  inventory_balance_after: number;
  location_balance_before: number;
  location_balance_after: number;
  transaction_id: string;
  damage_id: string | null;
}

export interface InventoryReturn {
  id: string;
  return_type: ReturnType;
  reference: string | null;
  party_name: string | null;
  status: "completed";
  employee_user_id: string;
  employee_name: string;
  created_by_user_id: string;
  created_by_name: string;
  reason: string;
  created_at: string;
  completed_at: string;
  lines: InventoryReturnLine[];
}

export interface CreateReturnRequest {
  return_type: ReturnType;
  reference?: string;
  party_name?: string;
  lines: Array<{
    barcode: string;
    quantity: number;
    location_code: string;
    condition?: StockCondition;
  }>;
  employee_badge_code: string;
  employee_pin?: string;
  idempotency_key: string;
  reason: string;
}

export type DamageCategory =
  | "physical_damage"
  | "quality_issue"
  | "expired"
  | "water_damage"
  | "other";

export type DamageResolution =
  | "restock"
  | "write_off"
  | "return_to_supplier";

export type DamageStatus =
  | "open"
  | "resolved";

export interface DamageCase {
  id: string;
  sku: string;
  barcode: string;
  product_name: string;
  location_code: string;
  location_name: string;
  location_path: string;
  quantity: number;
  remaining_quantity: number;
  category: DamageCategory;
  reference: string | null;
  reason: string;
  status: DamageStatus;
  resolution: DamageResolution | null;
  resolution_reason: string | null;
  source_type: string | null;
  source_id: string | null;
  reported_by_user_id: string;
  reported_by_name: string;
  created_by_user_id: string;
  created_by_name: string;
  resolved_by_user_id: string | null;
  resolved_by_name: string | null;
  resolved_actor_user_id: string | null;
  resolved_actor_name: string | null;
  inventory_balance_at_report: number;
  location_balance_at_report: number;
  inventory_balance_after_resolution: number | null;
  location_balance_after_resolution: number | null;
  resolution_transaction_id: string | null;
  created_at: string;
  resolved_at: string | null;
}

export interface CreateDamageRequest {
  barcode: string;
  location_code: string;
  quantity: number;
  category?: DamageCategory;
  reference?: string;
  employee_badge_code: string;
  employee_pin?: string;
  reason: string;
  idempotency_key: string;
}

export interface ResolveDamageRequest {
  resolution: DamageResolution;
  employee_badge_code: string;
  employee_pin?: string;
  reason: string;
  idempotency_key: string;
}

export type InvestigationKind =
  | "missing"
  | "misplaced"
  | "unexpected";

export type InvestigationStatus =
  | "open"
  | "resolved";

export type InvestigationResolution =
  | "located"
  | "confirmed_missing"
  | "record_error"
  | "dismissed";

export type InvestigationEventType =
  | "opened"
  | "evidence_added"
  | "resolved";

export interface InvestigationEvent {
  id: string;
  sequence: number;
  event_type: InvestigationEventType;
  location_code: string | null;
  location_name: string | null;
  location_path: string | null;
  reference: string | null;
  note: string | null;
  reason: string | null;
  resolution: InvestigationResolution | null;
  employee_user_id: string;
  employee_name: string;
  actor_user_id: string;
  actor_name: string;
  created_at: string;
}

export interface InventoryInvestigation {
  id: string;
  kind: InvestigationKind;
  status: InvestigationStatus;
  sku: string;
  barcode: string;
  product_name: string;
  quantity: number;
  expected_location_code: string | null;
  expected_location_name: string | null;
  expected_location_path: string | null;
  observed_location_code: string | null;
  observed_location_name: string | null;
  observed_location_path: string | null;
  reference: string | null;
  note: string | null;
  inventory_balance_at_open: number;
  inventory_version_at_open: number;
  expected_location_balance_at_open: number | null;
  expected_location_version_at_open: number | null;
  observed_location_balance_at_open: number | null;
  observed_location_version_at_open: number | null;
  opened_employee_user_id: string;
  opened_employee_name: string;
  opened_actor_user_id: string;
  opened_actor_name: string;
  resolution: InvestigationResolution | null;
  resolution_reference: string | null;
  resolution_reason: string | null;
  resolved_location_code: string | null;
  resolved_location_name: string | null;
  resolved_location_path: string | null;
  resolved_employee_user_id: string | null;
  resolved_employee_name: string | null;
  resolved_actor_user_id: string | null;
  resolved_actor_name: string | null;
  created_at: string;
  resolved_at: string | null;
  events: InvestigationEvent[];
}

export interface CreateInvestigationRequest {
  kind: InvestigationKind;
  barcode: string;
  quantity: number;
  expected_location_code?: string | null;
  observed_location_code?: string | null;
  reference?: string | null;
  employee_badge_code: string;
  employee_pin?: string | null;
  note?: string | null;
  idempotency_key: string;
}

export interface AddInvestigationEvidenceRequest {
  location_code?: string | null;
  note: string;
  employee_badge_code: string;
  employee_pin?: string | null;
  idempotency_key: string;
}

export interface ResolveInvestigationRequest {
  resolution: InvestigationResolution;
  resolved_location_code?: string | null;
  reference?: string | null;
  employee_badge_code: string;
  employee_pin?: string | null;
  reason: string;
  idempotency_key: string;
}

export interface InvestigationResponse {
  data: InventoryInvestigation;
  replayed: boolean;
}

export type ReservationStatus =
  | "active"
  | "released"
  | "fulfilled";

export type ReservationAction =
  | "release"
  | "fulfill";

export interface ReservationLine {
  sku: string;
  barcode: string;
  product_name: string;
  location_code: string;
  location_name: string;
  location_path: string;
  quantity: number;
  on_hand_at_create: number;
  reserved_before: number;
  reserved_after: number;
  available_before: number;
  available_after: number;
  inventory_balance_after_fulfillment: number | null;
  location_balance_after_fulfillment: number | null;
  fulfillment_transaction_id: string | null;
}

export interface InventoryReservation {
  id: string;
  reference: string;
  customer_name: string | null;
  sales_channel: string | null;
  status: ReservationStatus;
  note: string | null;
  close_action: ReservationAction | null;
  close_reason: string | null;
  created_by_user_id: string;
  created_by_name: string;
  closed_by_user_id: string | null;
  closed_by_name: string | null;
  closed_actor_user_id: string | null;
  closed_actor_name: string | null;
  created_at: string;
  closed_at: string | null;
  total_quantity: number;
  lines: ReservationLine[];
}

export interface CreateReservationRequest {
  reference: string;
  customer_name?: string;
  sales_channel?: string;
  lines: Array<{
    barcode: string;
    location_code: string;
    quantity: number;
  }>;
  note?: string;
  idempotency_key: string;
}

export interface CloseReservationRequest {
  action: ReservationAction;
  employee_badge_code?: string;
  employee_pin?: string;
  reason?: string;
  idempotency_key: string;
}

export type OrderComparisonStatus =
  | "matched"
  | "missing_removal"
  | "quantity_mismatch"
  | "unknown_product";

export type OrderComparisonResolution =
  | "link_removal"
  | "source_corrected"
  | "accept_exception";

export type OrderComparisonMatchSource =
  | "reservation"
  | "manual_link"
  | "none";

export type OrderComparisonEvidenceIssue =
  | "ambiguous_order_reference"
  | "incomplete_fulfillment"
  | "reversed_removal"
  | "inconsistent_fulfillment"
  | "quantity_limit_exceeded";

export type OrderComparisonProductIssue =
  | "product_identifier_conflict";

export interface OrderComparisonLinkedTransaction {
  id: string;
  quantity: number;
  reason: string | null;
  actor_name: string | null;
  created_at: string | null;
  reversed: boolean;
  valid: boolean;
}

export interface OrderComparisonLine {
  id: string;
  source_row_number: number;
  order_reference: string;
  submitted_sku: string | null;
  submitted_barcode: string | null;
  sku: string | null;
  barcode: string | null;
  product_name: string | null;
  current_product_sku: string | null;
  product_mapping_issue: OrderComparisonProductIssue | null;
  ordered_quantity: number;
  stockpile_quantity: number;
  effective_stockpile_quantity: number;
  comparison_status: OrderComparisonStatus;
  current_comparison_status: OrderComparisonStatus;
  match_source: OrderComparisonMatchSource;
  reservation_id: string | null;
  current_reservation_id: string | null;
  reservation_status: ReservationStatus | null;
  current_reservation_quantity: number;
  evidence_issue: OrderComparisonEvidenceIssue | null;
  resolution: OrderComparisonResolution | null;
  resolution_reason: string | null;
  resolved_by_user_id: string | null;
  resolved_by_name: string | null;
  resolved_at: string | null;
  linked_transaction: OrderComparisonLinkedTransaction | null;
  resolved: boolean;
  stale: boolean;
  needs_attention: boolean;
  created_at: string;
}

export interface OrderComparisonSummary {
  lines: number;
  matched: number;
  current_matched: number;
  issues: number;
  open_issues: number;
  resolved_issues: number;
  stale: number;
}

export interface OrderComparisonBatch {
  id: string;
  sales_channel: string;
  source_filename: string | null;
  imported_by_user_id: string;
  imported_by_name: string;
  created_at: string;
  checked_at: string;
  summary: OrderComparisonSummary;
  lines: OrderComparisonLine[];
}

export type ResolveOrderComparisonRequest =
  | {
      resolution: "link_removal";
      inventory_transaction_id: string;
      reason: string;
      idempotency_key: string;
    }
  | {
      resolution: "source_corrected" | "accept_exception";
      inventory_transaction_id?: never;
      reason: string;
      idempotency_key: string;
    };

export interface OrderComparisonResponse {
  authority: string;
  data: OrderComparisonBatch;
  replayed: boolean;
}

export type AIConfidence =
  | "low"
  | "medium"
  | "high";

export type AIReviewPriority =
  | "low"
  | "medium"
  | "high";

export type DeliveryDocumentReadStatus =
  | "readable"
  | "partial"
  | "unreadable";

export type DeliveryDocumentType =
  | "delivery_receipt"
  | "invoice"
  | "purchase_order"
  | "unknown";

export type DeliveryDocumentMatchStatus =
  | "matched"
  | "unknown_product"
  | "identifier_conflict"
  | "inactive_product"
  | "invalid_quantity"
  | "duplicate_product";

export interface DeliveryDocumentExtractedLine {
  source_line: number;
  sku: string | null;
  barcode: string | null;
  description: string | null;
  quantity: number | null;
  unit: string | null;
  source_text: string;
  confidence: AIConfidence;
}

export interface DeliveryDocumentExtraction {
  read_status: DeliveryDocumentReadStatus;
  document_type: DeliveryDocumentType;
  supplier_name: string | null;
  reference: string | null;
  document_date: string | null;
  lines: DeliveryDocumentExtractedLine[];
  warnings: string[];
  confidence: AIConfidence;
}

export interface DeliveryDocumentProduct {
  sku: string;
  barcode: string;
  product_name: string;
  unit: string;
}

export interface DeliveryDocumentLineMatch {
  source_line: number;
  status: DeliveryDocumentMatchStatus;
  product: DeliveryDocumentProduct | null;
}

export interface DeliveryDocumentAnalysis {
  extraction: DeliveryDocumentExtraction;
  matches: DeliveryDocumentLineMatch[];
  receipt_ready: boolean;
}

export interface AIDiscrepancyFinding {
  priority: AIReviewPriority;
  title: string;
  explanation: string;
  evidence_refs: string[];
  next_step: string;
  confidence: AIConfidence;
}

export type AIDiscrepancyReviewStatus =
  | "ready"
  | "no_issues"
  | "insufficient_evidence";

export interface AIDiscrepancyReview {
  review_status: AIDiscrepancyReviewStatus;
  summary: string;
  findings: AIDiscrepancyFinding[];
  questions_for_staff: string[];
  limitations: string[];
  confidence: AIConfidence;
}

export type AIReviewType =
  | "delivery_document"
  | "discrepancy_review";

export type AIReviewRunStatus =
  | "processing"
  | "completed"
  | "failed";

export type AIReviewProvider =
  | "together"
  | "stockpile";

export interface AIReviewSource {
  filename: string | null;
  media_type: string | null;
  size_bytes: number | null;
  sha256: string | null;
}

export interface AIReviewErrorState {
  code: string;
  message: string;
}

export interface AIReviewUsage {
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  latency_ms: number | null;
  provider_request_id: string | null;
}

export interface AIReviewBase<TResult> {
  id: string;
  review_type: AIReviewType;
  status: AIReviewRunStatus;
  provider: AIReviewProvider;
  model: string;
  prompt_version: string;
  source: AIReviewSource | null;
  window_hours: number | null;
  evidence_count: number;
  omitted_evidence_count: number;
  result: TResult | null;
  error: AIReviewErrorState | null;
  attempts: number;
  usage: AIReviewUsage;
  requested_by_user_id: string;
  requested_by_name: string;
  created_at: string;
  last_attempt_at: string | null;
  completed_at: string | null;
}

export type DeliveryDocumentAIReview =
  AIReviewBase<DeliveryDocumentAnalysis> & {
    review_type: "delivery_document";
    source: AIReviewSource;
    window_hours: null;
  };

export type DiscrepancyAIReview =
  AIReviewBase<AIDiscrepancyReview> & {
    review_type: "discrepancy_review";
    source: null;
    window_hours: 24;
  };

export type AIReviewRecord =
  | DeliveryDocumentAIReview
  | DiscrepancyAIReview;

export interface AIReviewResponse<
  TReview extends AIReviewRecord = AIReviewRecord,
> {
  authority: string;
  data: TReview;
  replayed: boolean;
}

export type StockCountStatus =
  | "pending"
  | "applied"
  | "cancelled";

export interface StockCountLine {
  sku: string;
  barcode: string;
  product_name: string;
  expected_quantity: number;
  counted_quantity: number;
  difference: number;
  inventory_balance_before: number;
  inventory_balance_after: number | null;
  location_balance_after: number | null;
  adjustment_transaction_id: string | null;
}

export interface InventoryStockCount {
  id: string;
  location_code: string;
  location_name: string;
  location_path: string;
  status: StockCountStatus;
  counter_user_id: string;
  counter_name: string;
  created_by_user_id: string;
  created_by_name: string;
  closed_by_user_id: string | null;
  closed_by_name: string | null;
  note: string | null;
  close_reason: string | null;
  created_at: string;
  closed_at: string | null;
  difference: number;
  discrepancy_lines: number;
  lines: StockCountLine[];
}

export interface CreateStockCountRequest {
  location_code: string;
  lines: Array<{
    barcode: string;
    quantity: number;
  }>;
  counter_badge_code: string;
  counter_pin?: string;
  idempotency_key: string;
  note?: string;
}

export interface ApproveStockCountRequest {
  idempotency_key: string;
  reason: string;
}

export interface CancelStockCountRequest {
  idempotency_key: string;
  reason: string;
}

export interface WorkflowResponse<T> {
  data: T;
  inventory: InventoryItem[];
  replayed: boolean;
}

export type DeliveryResponse = WorkflowResponse<InventoryDelivery>;
export type ReturnResponse = WorkflowResponse<InventoryReturn>;
export type DamageResponse = WorkflowResponse<DamageCase>;
export type ReservationResponse = WorkflowResponse<InventoryReservation>;
export type StockCountResponse = WorkflowResponse<InventoryStockCount>;

export type ExchangeStatus =
  | "pending"
  | "synced"
  | "failed"
  | "not_configured";

export interface ExchangeEvent {
  transaction_id: string;
  status: ExchangeStatus;
  attempts: number;
  last_error: string | null;
  created_at: string;
  last_attempt_at: string | null;
  synced_at: string | null;
  sku?: string;
  barcode?: string;
  movement_type?: Movement | "reversal";
  quantity?: number;
  actor_name?: string;
  location_code?: string;
  location_name?: string;
  location_path?: string;
}

export interface TransactionResponse {
  data: InventoryTransaction;
  inventory: InventoryItem;
  exchange: ExchangeEvent;
  replayed: boolean;
}

export interface LoginResponse {
  token: string;
  user: User;
}
