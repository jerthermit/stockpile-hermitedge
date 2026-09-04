"use client";

import {
  ChangeEvent,
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { ApiError, stockpileApi } from "../lib/api";
import type {
  InventoryTransaction,
  OrderComparisonBatch,
  OrderComparisonLine,
  OrderComparisonResolution,
  Role,
} from "../lib/types";

const MAX_ORDER_FILE_BYTES = 2_000_000;
const PAGE_SIZE = 24;

type LineFilter = "all" | "review";
type Notice = {
  tone: "success" | "error";
  text: string;
};

interface ReviewDraft {
  lineId: string;
  resolution: OrderComparisonResolution;
  transactionId: string;
  reason: string;
  key: string;
}

interface ImportAttempt {
  signature: string;
  key: string;
}

interface OrderComparisonPanelProps {
  token: string;
  role: Role;
  transactions: InventoryTransaction[];
  onUnauthorized?: () => void;
  embedded?: boolean;
}

function readableError(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function formatDate(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown date";

  return new Intl.DateTimeFormat("en-PH", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function quantity(value: number) {
  return value.toLocaleString("en-PH");
}

function statusLabel(line: OrderComparisonLine) {
  if (line.stale) return "Changed";
  if (line.resolved) {
    if (line.resolution === "link_removal") return "Removal linked";
    if (line.resolution === "source_corrected") return "Order corrected";
    return "Accepted";
  }
  if (line.product_mapping_issue) return "Product changed";
  if (line.evidence_issue) return "Record issue";
  if (line.current_comparison_status === "matched") return "Matched";
  if (line.current_comparison_status === "missing_removal") return "Missing";
  if (line.current_comparison_status === "quantity_mismatch") {
    return "Count differs";
  }
  return "Unknown item";
}

function statusTone(line: OrderComparisonLine) {
  if (line.stale || line.needs_attention) return "warning";
  if (line.current_comparison_status === "matched" || line.resolved) {
    return "success";
  }
  return "neutral";
}

function statusNote(line: OrderComparisonLine) {
  if (line.stale) return "Run a fresh check";
  if (line.product_mapping_issue) return "Identifiers no longer agree";
  if (line.evidence_issue === "ambiguous_order_reference") {
    return "Duplicate order reference";
  }
  if (line.evidence_issue === "incomplete_fulfillment") {
    return "Removal is incomplete";
  }
  if (line.evidence_issue === "reversed_removal") {
    return "Removal was reversed";
  }
  if (line.evidence_issue === "inconsistent_fulfillment") {
    return "Removal record differs";
  }
  if (line.evidence_issue === "quantity_limit_exceeded") {
    return "Quantity exceeds the limit";
  }
  if (line.resolved && line.resolved_by_name) {
    return line.resolved_by_name;
  }
  return null;
}

function productLabel(line: OrderComparisonLine) {
  return (
    line.product_name ||
    line.submitted_sku ||
    line.submitted_barcode ||
    "Unknown item"
  );
}

function productCode(line: OrderComparisonLine) {
  return line.sku || line.submitted_sku || line.submitted_barcode || "—";
}

function canLinkRemoval(line: OrderComparisonLine) {
  return (
    line.ordered_quantity > line.stockpile_quantity &&
    Boolean(line.sku) &&
    !line.evidence_issue &&
    !line.product_mapping_issue &&
    !line.stale
  );
}

function recentRemovalSuggestions(
  line: OrderComparisonLine,
  transactions: InventoryTransaction[],
) {
  const deficit = line.ordered_quantity - line.stockpile_quantity;
  if (!canLinkRemoval(line)) {
    return [];
  }

  return transactions.filter(
    (transaction) =>
      transaction.sku === line.sku &&
      transaction.movement_type === "stock_out" &&
      ["scanner", "manual"].includes(transaction.source) &&
      transaction.quantity === deficit &&
      !transaction.reversed_by_transaction_id,
  );
}

function importSignature(file: File, channel: string) {
  return [
    channel.trim().toLocaleLowerCase("en"),
    file.name,
    file.size,
    file.lastModified,
  ].join("\u001f");
}

function newImportKey() {
  if (globalThis.crypto?.randomUUID) {
    return `order-import-${globalThis.crypto.randomUUID()}`;
  }

  return `order-import-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function resolutionKey(lineId: string) {
  if (globalThis.crypto?.randomUUID) {
    return `order-resolution-${globalThis.crypto.randomUUID()}`;
  }

  return `order-resolution-${lineId}-${Date.now()}`.slice(0, 128);
}

export default function OrderComparisonPanel({
  token,
  role,
  transactions,
  onUnauthorized,
  embedded = false,
}: OrderComparisonPanelProps) {
  const [batches, setBatches] = useState<OrderComparisonBatch[]>([]);
  const [activeBatchId, setActiveBatchId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [salesChannel, setSalesChannel] = useState("Shopee");
  const [importing, setImporting] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<LineFilter>("all");
  const [page, setPage] = useState(1);
  const [review, setReview] = useState<ReviewDraft | null>(null);
  const [saving, setSaving] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const importAttempt = useRef<ImportAttempt | null>(null);

  const handleError = useCallback(
    (caught: unknown, fallback: string) => {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized?.();
        return "Your session expired.";
      }
      return readableError(caught, fallback);
    },
    [onUnauthorized],
  );

  const loadBatches = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await stockpileApi.orderComparisons(token);
      setBatches(next);
      setActiveBatchId((current) =>
        current && next.some((batch) => batch.id === current)
          ? current
          : next[0]?.id || null,
      );
    } catch (caught) {
      setError(handleError(caught, "Order checks could not be loaded."));
    } finally {
      setLoading(false);
    }
  }, [handleError, token]);

  useEffect(() => {
    void loadBatches();
  }, [loadBatches]);

  useEffect(() => {
    setPage(1);
    setReview(null);
  }, [activeBatchId, filter, query]);

  const activeBatch = useMemo(
    () =>
      batches.find((batch) => batch.id === activeBatchId) || batches[0] || null,
    [activeBatchId, batches],
  );

  const filteredLines = useMemo(() => {
    if (!activeBatch) return [];
    const needle = query.trim().toLocaleLowerCase("en");

    return activeBatch.lines.filter((line) => {
      if (filter === "review" && !line.needs_attention) return false;
      if (!needle) return true;

      return [
        line.order_reference,
        line.product_name,
        line.sku,
        line.barcode,
        line.submitted_sku,
        line.submitted_barcode,
      ].some((value) => value?.toLocaleLowerCase("en").includes(needle));
    });
  }, [activeBatch, filter, query]);

  const totalPages = Math.max(1, Math.ceil(filteredLines.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const pageLines = filteredLines.slice(
    (safePage - 1) * PAGE_SIZE,
    safePage * PAGE_SIZE,
  );
  const rangeStart = filteredLines.length
    ? (safePage - 1) * PAGE_SIZE + 1
    : 0;
  const rangeEnd = Math.min(safePage * PAGE_SIZE, filteredLines.length);

  const reviewLine = useMemo(
    () =>
      review && activeBatch
        ? activeBatch.lines.find((line) => line.id === review.lineId) || null
        : null,
    [activeBatch, review],
  );
  const removalOptions = useMemo(
    () =>
      reviewLine ? recentRemovalSuggestions(reviewLine, transactions) : [],
    [reviewLine, transactions],
  );

  async function handleOrderFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!file) return;

    const channel = salesChannel.trim();
    if (!channel) {
      setNotice({ tone: "error", text: "Enter the selling app name." });
      return;
    }
    if (!file.name.toLocaleLowerCase("en").endsWith(".csv")) {
      setNotice({ tone: "error", text: "Choose a CSV file." });
      return;
    }
    if (file.size > MAX_ORDER_FILE_BYTES) {
      setNotice({ tone: "error", text: "CSV must be smaller than 2 MB." });
      return;
    }

    const signature = importSignature(file, channel);
    if (importAttempt.current?.signature !== signature) {
      importAttempt.current = { signature, key: newImportKey() };
    }
    const attemptKey = importAttempt.current.key;

    setImporting(true);
    setNotice(null);
    try {
      const response = await stockpileApi.importSellingOrders(
        token,
        attemptKey,
        channel,
        file,
      );
      importAttempt.current = null;
      setBatches((current) => [
        response.data,
        ...current.filter((batch) => batch.id !== response.data.id),
      ]);
      setActiveBatchId(response.data.id);
      await loadBatches();
      setActiveBatchId(response.data.id);
      setFilter(response.data.summary.open_issues ? "review" : "all");
      setNotice({
        tone: "success",
        text: response.replayed ? "Existing check opened." : "Order check complete.",
      });
    } catch (caught) {
      setNotice({
        tone: "error",
        text: handleError(caught, "Order check failed."),
      });
    } finally {
      setImporting(false);
    }
  }

  function openReview(line: OrderComparisonLine) {
    const options = recentRemovalSuggestions(line, transactions);
    const canLink = canLinkRemoval(line);
    setReview({
      lineId: line.id,
      resolution: canLink ? "link_removal" : "source_corrected",
      transactionId: options[0]?.id || "",
      reason: "",
      key: resolutionKey(line.id),
    });
    setNotice(null);
  }

  async function submitReview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!review || !reviewLine) return;

    const reason = review.reason.trim();
    if (reason.length < 3) {
      setNotice({ tone: "error", text: "Add a short review note." });
      return;
    }
    if (review.resolution === "link_removal" && !review.transactionId.trim()) {
      setNotice({ tone: "error", text: "Choose the stock removal." });
      return;
    }

    setSaving(true);
    setNotice(null);
    try {
      const response =
        review.resolution === "link_removal"
          ? await stockpileApi.resolveOrderComparison(
              token,
              review.lineId,
              review.key,
              {
                resolution: "link_removal",
                inventory_transaction_id: review.transactionId.trim(),
                reason,
              },
            )
          : await stockpileApi.resolveOrderComparison(
              token,
              review.lineId,
              review.key,
              {
                resolution: review.resolution,
                reason,
              },
            );

      setBatches((current) =>
        current.map((batch) =>
          batch.id === response.data.id ? response.data : batch,
        ),
      );
      setReview(null);
      setNotice({ tone: "success", text: "Review saved." });
    } catch (caught) {
      setNotice({
        tone: "error",
        text: handleError(caught, "Review could not be saved."),
      });
    } finally {
      setSaving(false);
    }
  }

  return (
    <section
      className={`order-check-panel${embedded ? " embedded-panel" : ""}`}
      aria-label="Selling orders"
    >
      <header className="order-check-head">
        <div className="order-check-title">
          <h2>Order check</h2>
          {activeBatch && <span>{formatDate(activeBatch.checked_at)}</span>}
        </div>

        <div className="order-check-actions">
          {role === "admin" && (
            <>
              <label className="sr-only" htmlFor="order-sales-channel">
                Selling app
              </label>
              <input
                id="order-sales-channel"
                className="order-channel-input"
                value={salesChannel}
                onChange={(event: ChangeEvent<HTMLInputElement>) =>
                  setSalesChannel(event.target.value)
                }
                maxLength={80}
                placeholder="Selling app"
                disabled={importing}
              />
              <input
                ref={fileInput}
                type="file"
                accept=".csv,text/csv"
                onChange={handleOrderFile}
                hidden
              />
              <button
                type="button"
                className="button button-primary"
                onClick={() => fileInput.current?.click()}
                disabled={importing}
              >
                {importing ? "Checking…" : "Check CSV"}
              </button>
            </>
          )}
        </div>
      </header>

      {notice && (
        <div
          className={`alert ${notice.tone === "error" ? "alert-error" : "alert-success"}`}
          role={notice.tone === "error" ? "alert" : "status"}
        >
          <span>{notice.text}</span>
          <button type="button" className="text-button" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </div>
      )}

      {error && batches.length > 0 && (
        <div className="alert alert-error" role="alert">
          <span>{error}</span>
          <button type="button" className="text-button" onClick={loadBatches}>
            Retry
          </button>
        </div>
      )}

      {loading && !batches.length ? (
        <div className="loading-state" role="status">
          <span className="spinner" aria-hidden="true" /> Loading…
        </div>
      ) : error && !batches.length ? (
        <div className="empty-state" role="alert">
          <strong>Order checks unavailable</strong>
          <button type="button" className="button button-secondary" onClick={loadBatches}>
            Retry
          </button>
        </div>
      ) : !activeBatch ? (
        <div className="empty-state">
          <strong>No order checks</strong>
        </div>
      ) : (
        <>
          <div className="order-batch-bar">
            <div className="order-batch-picker">
              <label className="sr-only" htmlFor="order-batch">
                Order check
              </label>
              <select
                id="order-batch"
                value={activeBatch.id}
                onChange={(event: ChangeEvent<HTMLSelectElement>) =>
                  setActiveBatchId(event.target.value)
                }
              >
                {batches.map((batch) => (
                  <option key={batch.id} value={batch.id}>
                    {batch.sales_channel} · {formatDate(batch.created_at)}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="text-button"
                onClick={loadBatches}
                disabled={loading}
              >
                Refresh
              </button>
            </div>

            <div className="order-summary" aria-label="Check summary">
              <span><strong>{quantity(activeBatch.summary.lines)}</strong> Lines</span>
              <span><strong>{quantity(activeBatch.summary.current_matched)}</strong> Matched</span>
              <span className={activeBatch.summary.open_issues ? "has-issues" : ""}>
                <strong>{quantity(activeBatch.summary.open_issues)}</strong> Review
              </span>
              {activeBatch.summary.stale > 0 && (
                <span className="has-issues">
                  <strong>{quantity(activeBatch.summary.stale)}</strong> Changed
                </span>
              )}
            </div>
          </div>

          <div className="order-table-tools">
            <label className="order-search">
              <span className="sr-only">Search orders</span>
              <input
                type="search"
                value={query}
                onChange={(event: ChangeEvent<HTMLInputElement>) =>
                  setQuery(event.target.value)
                }
                placeholder="Search order or item"
              />
            </label>

            <div className="order-filter" role="group" aria-label="Order lines">
              <button
                type="button"
                className={filter === "all" ? "active" : ""}
                onClick={() => setFilter("all")}
              >
                All
              </button>
              <button
                type="button"
                className={filter === "review" ? "active" : ""}
                onClick={() => setFilter("review")}
              >
                Review
              </button>
            </div>

            <div className="order-pagination" aria-label="Order pages">
              <span>
                {quantity(rangeStart)}–{quantity(rangeEnd)} of {quantity(filteredLines.length)}
              </span>
              <button
                type="button"
                className="text-button"
                onClick={() => setPage((current) => Math.max(1, current - 1))}
                disabled={safePage === 1}
                aria-label="Previous order page"
              >
                Previous
              </button>
              <button
                type="button"
                className="text-button"
                onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
                disabled={safePage === totalPages}
                aria-label="Next order page"
              >
                Next
              </button>
            </div>
          </div>

          <div className="order-table-scroll" tabIndex={0}>
            <table className="order-table">
              <thead>
                <tr>
                  <th>Order</th>
                  <th>Item</th>
                  <th className="numeric">Ordered</th>
                  <th className="numeric">Stockpile</th>
                  <th>Status</th>
                  {role === "admin" && <th aria-label="Review action" />}
                </tr>
              </thead>
              <tbody>
                {pageLines.map((line) => {
                  const note = statusNote(line);
                  const canReview =
                    role === "admin" &&
                    line.needs_attention &&
                    !line.stale &&
                    !line.resolved;

                  return (
                    <tr key={line.id} className={line.needs_attention ? "needs-review" : ""}>
                      <td>
                        <strong>{line.order_reference}</strong>
                        <small>Row {quantity(line.source_row_number)}</small>
                      </td>
                      <td>
                        <strong>{productLabel(line)}</strong>
                        <small>{productCode(line)}</small>
                      </td>
                      <td className="numeric">{quantity(line.ordered_quantity)}</td>
                      <td className="numeric">
                        {quantity(line.effective_stockpile_quantity)}
                      </td>
                      <td>
                        <span className={`order-state state-${statusTone(line)}`}>
                          {statusLabel(line)}
                        </span>
                        {note && <small>{note}</small>}
                      </td>
                      {role === "admin" && (
                        <td className="order-row-action">
                          {canReview ? (
                            <button
                              type="button"
                              className="text-button"
                              onClick={() => openReview(line)}
                            >
                              Review
                            </button>
                          ) : (
                            <span aria-hidden="true">—</span>
                          )}
                        </td>
                      )}
                    </tr>
                  );
                })}

                {!pageLines.length && (
                  <tr>
                    <td colSpan={role === "admin" ? 6 : 5} className="table-empty">
                      {filter === "review" ? "Nothing needs review." : "No matching orders."}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="order-batch-meta">
            <span>{activeBatch.source_filename || "Order CSV"}</span>
            <span>{activeBatch.imported_by_name}</span>
          </div>
        </>
      )}

      {review && reviewLine && (
        <form className="order-review" onSubmit={submitReview}>
          <header>
            <div>
              <span>Review</span>
              <strong>{reviewLine.order_reference}</strong>
            </div>
            <button
              type="button"
              className="text-button"
              onClick={() => setReview(null)}
              disabled={saving}
            >
              Close
            </button>
          </header>

          <div className="order-resolution-options" role="group" aria-label="Resolution">
            {canLinkRemoval(reviewLine) && (
              <button
                type="button"
                className={review.resolution === "link_removal" ? "active" : ""}
                onClick={() =>
                  setReview((current) =>
                    current
                      ? {
                          ...current,
                          resolution: "link_removal",
                          transactionId:
                            current.transactionId || removalOptions[0]?.id || "",
                        }
                      : current,
                  )
                }
              >
                Link removal
              </button>
            )}
            <button
              type="button"
              className={review.resolution === "source_corrected" ? "active" : ""}
              onClick={() =>
                setReview((current) =>
                  current ? { ...current, resolution: "source_corrected" } : current,
                )
              }
            >
              Order corrected
            </button>
            <button
              type="button"
              className={review.resolution === "accept_exception" ? "active" : ""}
              onClick={() =>
                setReview((current) =>
                  current ? { ...current, resolution: "accept_exception" } : current,
                )
              }
            >
              Accept variance
            </button>
          </div>

          {review.resolution === "link_removal" && (
            <label>
              Stock removal ID
              <input
                list={`order-removals-${reviewLine.id}`}
                value={review.transactionId}
                onChange={(event: ChangeEvent<HTMLInputElement>) =>
                  setReview((current) =>
                    current ? { ...current, transactionId: event.target.value } : current,
                  )
                }
                placeholder="Choose or paste a transaction ID"
                autoComplete="off"
                disabled={saving}
              />
              <datalist id={`order-removals-${reviewLine.id}`}>
                {removalOptions.map((transaction) => (
                  <option
                    key={transaction.id}
                    value={transaction.id}
                    label={`${formatDate(transaction.created_at)} · ${
                      transaction.location_name || transaction.location_code
                    } · ${transaction.reason || transaction.actor_name} · ${transaction.id.slice(0, 8)}`}
                  />
                ))}
              </datalist>
            </label>
          )}

          <label>
            Note
            <textarea
              value={review.reason}
              onChange={(event: ChangeEvent<HTMLTextAreaElement>) =>
                setReview((current) =>
                  current ? { ...current, reason: event.target.value } : current,
                )
              }
              rows={3}
              maxLength={500}
              disabled={saving}
            />
          </label>

          <footer>
            <button
              type="button"
              className="button button-ghost"
              onClick={() => setReview(null)}
              disabled={saving}
            >
              Cancel
            </button>
            <button type="submit" className="button button-primary" disabled={saving}>
              {saving ? "Saving…" : "Save review"}
            </button>
          </footer>
        </form>
      )}
    </section>
  );
}
