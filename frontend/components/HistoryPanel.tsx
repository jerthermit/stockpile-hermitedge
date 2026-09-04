"use client";

import {
  FormEvent,
  useMemo,
  useRef,
  useState,
} from "react";
import type {
  InventoryTransaction,
  Role,
} from "../lib/types";

interface HistoryPanelProps {
  transactions: InventoryTransaction[];
  role: Role;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onReverse: (
    id: string,
    reason: string,
    key: string,
  ) => Promise<void>;
  embedded?: boolean;
}

const PAGE_SIZE = 12;
const UNASSIGNED = "UNASSIGNED";

function newKey() {
  if (globalThis.crypto?.randomUUID) {
    return globalThis.crypto.randomUUID();
  }

  return `reverse-${Date.now()}-${Math.random()
    .toString(36)
    .slice(2)}`;
}

function formatTime(value: string) {
  const date = new Date(value);

  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("en-PH", {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: "Asia/Manila",
      }).format(date);
}

function locationLabel(
  transaction: InventoryTransaction,
) {
  if (
    transaction.location_code === UNASSIGNED
  ) {
    return "Location not recorded";
  }

  const name =
    transaction.location_path ||
    transaction.location_name ||
    transaction.location_code;

  return name === transaction.location_code
    ? transaction.location_code
    : `${transaction.location_code} — ${name}`;
}

function movementLabel(
  transaction: InventoryTransaction,
) {
  if (transaction.reverses_transaction_id) {
    return "Reversal";
  }

  return transaction.quantity_delta > 0
    ? "Added"
    : "Removed";
}

export default function HistoryPanel({
  transactions,
  role,
  loading,
  error,
  onRetry,
  onReverse,
  embedded = false,
}: HistoryPanelProps) {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [candidate, setCandidate] =
    useState<InventoryTransaction | null>(null);
  const [reason, setReason] = useState("");
  const [key, setKey] = useState("");
  const [submitting, setSubmitting] =
    useState(false);
  const [reversalError, setReversalError] =
    useState<string | null>(null);

  const lock = useRef(false);

  const reversedIds = useMemo(
    () =>
      new Set(
        transactions
          .map(
            (entry) =>
              entry.reverses_transaction_id,
          )
          .filter(
            (id): id is string =>
              typeof id === "string",
          ),
      ),
    [transactions],
  );

  const filteredTransactions = useMemo(() => {
    const needle = query.trim().toLowerCase();

    if (!needle) return transactions;

    return transactions.filter((transaction) =>
      [
        transaction.product_name,
        transaction.sku,
        transaction.actor_name,
        transaction.reason,
        transaction.id,
        transaction.location_code,
        transaction.location_name,
        transaction.location_path,
        locationLabel(transaction),
        movementLabel(transaction),
        formatTime(transaction.created_at),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [query, transactions]);

  const pageCount = Math.max(
    1,
    Math.ceil(
      filteredTransactions.length / PAGE_SIZE,
    ),
  );

  const safePage = Math.min(page, pageCount - 1);

  const pageTransactions =
    filteredTransactions.slice(
      safePage * PAGE_SIZE,
      safePage * PAGE_SIZE + PAGE_SIZE,
    );

  const firstVisible =
    filteredTransactions.length
      ? safePage * PAGE_SIZE + 1
      : 0;

  const lastVisible = Math.min(
    (safePage + 1) * PAGE_SIZE,
    filteredTransactions.length,
  );

  const reloadButton = (
    <button
      type="button"
      className="button button-secondary icon-button"
      onClick={onRetry}
      disabled={loading}
      aria-label="Reload movements"
      title="Reload movements"
    >
      <span aria-hidden="true">↻</span>
    </button>
  );

  const tools = (
    <div className="panel-tools">
      <label className="search-field">
        <span className="sr-only">
          Search movements
        </span>

        <input
          type="search"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setPage(0);
          }}
          placeholder="Search movements"
          autoComplete="off"
          spellCheck={false}
          aria-label="Search movements"
        />
      </label>

      {reloadButton}
    </div>
  );

  function openReversal(
    transaction: InventoryTransaction,
  ) {
    setCandidate(transaction);
    setReason("");
    setKey(newKey());
    setReversalError(null);
  }

  function closeReversal() {
    if (submitting) return;

    setCandidate(null);
    setReason("");
    setKey("");
    setReversalError(null);
  }

  async function submitReversal(
    event: FormEvent<HTMLFormElement>,
  ) {
    event.preventDefault();

    if (!candidate || lock.current) return;

    const cleanReason = reason.trim();

    if (cleanReason.length < 3) {
      setReversalError(
        "Enter a reason with at least 3 characters.",
      );
      return;
    }

    lock.current = true;
    setSubmitting(true);
    setReversalError(null);

    try {
      await onReverse(
        candidate.id,
        cleanReason,
        key,
      );

      setCandidate(null);
      setReason("");
      setKey("");
    } catch (reverseError) {
      setReversalError(
        reverseError instanceof Error
          ? reverseError.message
          : "Reversal failed.",
      );
    } finally {
      lock.current = false;
      setSubmitting(false);
    }
  }

  return (
    <section
      className={
        embedded
          ? "ledger-panel history-ledger"
          : "panel"
      }
      aria-label={
        embedded ? "Movements" : undefined
      }
      aria-labelledby={
        embedded ? undefined : "history-title"
      }
      aria-busy={loading}
    >
      {embedded ? (
        <div className="ledger-toolbar">
          {tools}
        </div>
      ) : (
        <div className="panel-heading panel-heading-wrap">
          <div className="title-with-count">
            <h2 id="history-title">
              Movements
            </h2>

            {!loading && !error && (
              <span>
                {transactions.length.toLocaleString(
                  "en-PH",
                )}
              </span>
            )}
          </div>

          {tools}
        </div>
      )}

      {loading ? (
        <div
          className="loading-state panel-state"
          role="status"
          aria-live="polite"
        >
          <span
            className="spinner"
            aria-hidden="true"
          />
          Loading…
        </div>
      ) : error ? (
        <div
          className="recovery-state"
          role="alert"
        >
          <div>
            <strong>
              Movements unavailable
            </strong>
            <span>{error}</span>
          </div>

          <button
            type="button"
            className="button button-secondary"
            onClick={onRetry}
          >
            Retry
          </button>
        </div>
      ) : transactions.length === 0 ? (
        <div className="empty-state">
          <strong>No movements</strong>
        </div>
      ) : filteredTransactions.length === 0 ? (
        <div className="empty-state compact">
          <strong>No matches</strong>
        </div>
      ) : (
        <div className="table-region">
          <div className="table-scroll">
            <table>
              <caption className="sr-only">
                Inventory movement history
              </caption>

              <thead>
                <tr>
                  <th scope="col">
                    Time / operator
                  </th>
                  <th scope="col">Item</th>
                  <th scope="col">Change</th>
                  <th scope="col">Location</th>
                  <th scope="col">
                    Stock total
                  </th>
                  <th scope="col">Note</th>

                  {role === "admin" && (
                    <th scope="col">
                      <span className="sr-only">
                        Actions
                      </span>
                    </th>
                  )}
                </tr>
              </thead>

              <tbody>
                {pageTransactions.map(
                  (transaction) => {
                    const reversed =
                      Boolean(
                        transaction.reversed_by_transaction_id,
                      ) ||
                      reversedIds.has(
                        transaction.id,
                      );

                    const isReversal = Boolean(
                      transaction.reverses_transaction_id,
                    );

                    return (
                      <tr key={transaction.id}>
                        <td>
                          <strong>
                            {formatTime(
                              transaction.created_at,
                            )}
                          </strong>

                          <span className="cell-subtext">
                            {transaction.actor_name}
                          </span>
                        </td>

                        <td>
                          <strong>
                            {
                              transaction.product_name
                            }
                          </strong>

                          <span className="cell-subtext mono">
                            {transaction.sku}
                          </span>
                        </td>

                        <td>
                          <span
                            className={`movement movement-${
                              transaction.quantity_delta >
                              0
                                ? "stock_in"
                                : "stock_out"
                            }`}
                          >
                            {transaction.quantity_delta >
                            0
                              ? "+"
                              : "−"}
                            {transaction.quantity.toLocaleString(
                              "en-PH",
                            )}
                          </span>

                          <span className="cell-subtext">
                            {reversed &&
                            !isReversal
                              ? "Reversed"
                              : movementLabel(
                                  transaction,
                                )}
                          </span>
                        </td>

                        <td>
                          <strong>
                            {locationLabel(
                              transaction,
                            )}
                          </strong>

                          <span className="cell-subtext mono">
                            {transaction.location_balance_before.toLocaleString(
                              "en-PH",
                            )}{" "}
                            →{" "}
                            {transaction.location_balance_after.toLocaleString(
                              "en-PH",
                            )}
                          </span>
                        </td>

                        <td className="mono">
                          {transaction.balance_before.toLocaleString(
                            "en-PH",
                          )}{" "}
                          →{" "}
                          {transaction.balance_after.toLocaleString(
                            "en-PH",
                          )}
                        </td>

                        <td>
                          {transaction.reason || (
                            <span className="muted">
                              —
                            </span>
                          )}
                        </td>

                        {role === "admin" && (
                          <td className="align-right">
                            {!reversed &&
                            !isReversal ? (
                              <button
                                type="button"
                                className="text-button danger-text"
                                onClick={() =>
                                  openReversal(
                                    transaction,
                                  )
                                }
                                aria-label={`Reverse movement for ${transaction.product_name}`}
                              >
                                Reverse
                              </button>
                            ) : (
                              <span className="cell-subtext">
                                {isReversal
                                  ? "Reversal"
                                  : "Reversed"}
                              </span>
                            )}
                          </td>
                        )}
                      </tr>
                    );
                  },
                )}
              </tbody>
            </table>
          </div>

          {filteredTransactions.length >
            PAGE_SIZE && (
            <div
              className="table-pagination"
              aria-label="Movement pages"
            >
              <span>
                {firstVisible}–{lastVisible} /{" "}
                {filteredTransactions.length.toLocaleString(
                  "en-PH",
                )}
              </span>

              <div className="button-row">
                <button
                  type="button"
                  className="button button-secondary"
                  onClick={() =>
                    setPage(
                      Math.max(0, safePage - 1),
                    )
                  }
                  disabled={safePage === 0}
                >
                  Previous
                </button>

                <button
                  type="button"
                  className="button button-secondary"
                  onClick={() =>
                    setPage(
                      Math.min(
                        pageCount - 1,
                        safePage + 1,
                      ),
                    )
                  }
                  disabled={
                    safePage >= pageCount - 1
                  }
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {candidate && (
        <div
          className="modal-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (
              event.currentTarget === event.target
            ) {
              closeReversal();
            }
          }}
        >
          <section
            className="modal-card"
            role="dialog"
            aria-modal="true"
            aria-labelledby="reverse-title"
            aria-describedby="reverse-note"
          >
            <h3 id="reverse-title">
              Reverse movement
            </h3>

            <p id="reverse-note">
              An opposite entry will be added. The
              original stays.
            </p>

            <dl className="review-grid">
              <div>
                <dt>Original</dt>
                <dd>
                  {candidate.quantity_delta > 0
                    ? "Added"
                    : "Removed"}{" "}
                  {candidate.quantity.toLocaleString(
                    "en-PH",
                  )}
                </dd>
              </div>

              <div>
                <dt>Location</dt>
                <dd>
                  {locationLabel(candidate)}
                </dd>
              </div>

              <div>
                <dt>At location</dt>
                <dd>
                  {candidate.location_balance_before.toLocaleString(
                    "en-PH",
                  )}{" "}
                  →{" "}
                  {candidate.location_balance_after.toLocaleString(
                    "en-PH",
                  )}
                </dd>
              </div>

              <div>
                <dt>Stock total</dt>
                <dd>
                  {candidate.balance_before.toLocaleString(
                    "en-PH",
                  )}{" "}
                  →{" "}
                  {candidate.balance_after.toLocaleString(
                    "en-PH",
                  )}
                </dd>
              </div>
            </dl>

            <form onSubmit={submitReversal}>
              <label htmlFor="reversal-reason">
                Reason
              </label>

              <textarea
                id="reversal-reason"
                name="reversal-reason"
                rows={3}
                value={reason}
                onChange={(event) => {
                  setReason(event.target.value);
                  setReversalError(null);
                }}
                placeholder="Why is this being reversed?"
                minLength={3}
                maxLength={240}
                required
                autoFocus
                disabled={submitting}
                aria-invalid={Boolean(
                  reversalError,
                )}
                aria-describedby={
                  reversalError
                    ? "reversal-error"
                    : undefined
                }
              />

              {reversalError && (
                <div
                  id="reversal-error"
                  className="alert alert-error"
                  role="alert"
                >
                  {reversalError}
                </div>
              )}

              <div className="button-row">
                <button
                  type="button"
                  className="button button-secondary"
                  onClick={closeReversal}
                  disabled={submitting}
                >
                  Cancel
                </button>

                <button
                  type="submit"
                  className="button button-danger"
                  disabled={
                    submitting ||
                    reason.trim().length < 3
                  }
                >
                  {submitting
                    ? "Recording…"
                    : "Record reversal"}
                </button>
              </div>
            </form>
          </section>
        </div>
      )}
    </section>
  );
}