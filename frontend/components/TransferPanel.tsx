"use client";

import {
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { stockpileApi } from "../lib/api";
import type {
  InventoryItem,
  InventoryTransfer,
  StockLocation,
} from "../lib/types";

interface TransferPanelProps {
  token: string;
  items: InventoryItem[];
  locations: StockLocation[];
  onChanged: () => void | Promise<void>;
  embedded?: boolean;
}

interface DraftLine {
  item: InventoryItem;
  quantity: number;
  available: number;
}

interface ReceiptLineDraft {
  barcode: string;
  quantity: string;
  note: string;
}

interface SubmissionAttempt {
  signature: string;
  key: string;
}

const UNASSIGNED = "UNASSIGNED";

function newSubmissionKey(prefix: string) {
  const suffix =
    globalThis.crypto?.randomUUID?.() ??
    String(Date.now()) + "-" + Math.random().toString(16).slice(2);
  return (prefix + "-" + suffix).slice(0, 128);
}

function attemptKey(
  attempt: { current: SubmissionAttempt | null },
  signature: string,
  prefix: string,
) {
  if (attempt.current?.signature === signature) {
    return attempt.current.key;
  }
  const key = newSubmissionKey(prefix);
  attempt.current = { signature, key };
  return key;
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

function locationLabel(location: StockLocation) {
  const path = location.path || location.name || location.code;
  return path === location.code
    ? location.code
    : location.code + " — " + path;
}

function productMark(item: Pick<InventoryItem, "product_name" | "variant">) {
  const words = (item.variant || item.product_name)
    .replace(/[^A-Za-z0-9 ]/g, " ")
    .split(/\s+/)
    .filter(Boolean);
  return (
    words
      .slice(0, 2)
      .map((word) => word[0])
      .join("")
      .toUpperCase() || "ST"
  );
}

function transferCode(id: string) {
  return id.slice(0, 8).toUpperCase();
}

function statusLabel(status: InventoryTransfer["status"]) {
  if (status === "received_with_discrepancy") return "Check count";
  if (status === "received") return "Received";
  if (status === "cancelled") return "Cancelled";
  return "Pending";
}

function quantityAt(item: InventoryItem, locationCode: string) {
  return (
    (item.positions ?? []).find(
      (position) => position.location_code === locationCode,
    )?.quantity ?? 0
  );
}

export default function TransferPanel({
  token,
  items,
  locations,
  onChanged,
  embedded = false,
}: TransferPanelProps) {
  const [transfers, setTransfers] = useState<InventoryTransfer[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [sourceCode, setSourceCode] = useState("");
  const [destinationCode, setDestinationCode] = useState("");
  const [barcode, setBarcode] = useState("");
  const [quantity, setQuantity] = useState("1");
  const [note, setNote] = useState("");
  const [draftLines, setDraftLines] = useState<DraftLine[]>([]);

  const [receivingTransfer, setReceivingTransfer] =
    useState<InventoryTransfer | null>(null);
  const [receiptLines, setReceiptLines] = useState<ReceiptLineDraft[]>([]);
  const [receiverBadge, setReceiverBadge] = useState("");
  const [receiverPin, setReceiverPin] = useState("");
  const [receiveNote, setReceiveNote] = useState("");
  const [receiveError, setReceiveError] = useState<string | null>(null);
  const [receiving, setReceiving] = useState(false);

  const barcodeInput = useRef<HTMLInputElement>(null);
  const badgeInput = useRef<HTMLInputElement>(null);
  const createAttempt = useRef<SubmissionAttempt | null>(null);
  const receiveAttempt = useRef<SubmissionAttempt | null>(null);

  const stockableLocations = useMemo(
    () =>
      locations
        .filter(
          (location) =>
            location.active &&
            location.stockable &&
            location.code !== UNASSIGNED,
        )
        .sort((left, right) =>
          locationLabel(left).localeCompare(locationLabel(right)),
        ),
    [locations],
  );

  const sourceLocations = useMemo(
    () =>
      stockableLocations.filter((location) =>
        items.some(
          (item) =>
            item.active !== false && quantityAt(item, location.code) > 0,
        ),
      ),
    [items, stockableLocations],
  );

  const destinationLocations = useMemo(
    () =>
      stockableLocations.filter((location) => location.code !== sourceCode),
    [sourceCode, stockableLocations],
  );

  const pendingTransfers = useMemo(
    () => transfers.filter((transfer) => transfer.status === "pending"),
    [transfers],
  );

  const recentTransfers = useMemo(
    () =>
      transfers
        .filter((transfer) => transfer.status !== "pending")
        .slice(0, 8),
    [transfers],
  );

  const loadTransfers = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      setTransfers(await stockpileApi.transfers(token));
    } catch (error) {
      setLoadError(
        error instanceof Error ? error.message : "Transfers could not be loaded.",
      );
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void loadTransfers();
  }, [loadTransfers]);

  useEffect(() => {
    if (!createOpen && !receivingTransfer) return;

    function closeOnEscape(event: KeyboardEvent) {
      if (event.key !== "Escape" || creating || receiving) return;
      if (receivingTransfer) {
        closeReceipt();
      } else {
        closeCreate();
      }
    }

    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [createOpen, creating, receiving, receivingTransfer]);

  function resetCreate() {
    setSourceCode("");
    setDestinationCode("");
    setBarcode("");
    setQuantity("1");
    setNote("");
    setDraftLines([]);
    setCreateError(null);
    createAttempt.current = null;
  }

  function openCreate() {
    setNotice(null);
    resetCreate();
    setCreateOpen(true);
  }

  function closeCreate() {
    if (creating) return;
    setCreateOpen(false);
    resetCreate();
  }

  function addLine() {
    setCreateError(null);
    if (!sourceCode) {
      setCreateError("Choose the source location.");
      return;
    }

    const productCode = barcode.trim();
    const parsedQuantity = Number(quantity);
    if (!productCode) {
      setCreateError("Scan a barcode.");
      return;
    }
    if (
      !Number.isInteger(parsedQuantity) ||
      parsedQuantity <= 0 ||
      parsedQuantity > 1_000_000
    ) {
      setCreateError("Enter a valid whole quantity.");
      return;
    }

    const normalized = productCode.toLowerCase();
    const item = items.find(
      (candidate) =>
        candidate.active !== false &&
        (candidate.barcode.toLowerCase() === normalized ||
          candidate.sku.toLowerCase() === normalized),
    );
    if (!item) {
      setCreateError("Product not found.");
      return;
    }
    if (draftLines.some((line) => line.item.sku === item.sku)) {
      setCreateError("This product is already in the handoff.");
      return;
    }

    const available = quantityAt(item, sourceCode);
    if (available <= 0) {
      setCreateError("This product is not at the source location.");
      return;
    }
    if (parsedQuantity > available) {
      setCreateError(
        "Only " + available.toLocaleString("en-PH") + " available.",
      );
      return;
    }

    setDraftLines((current) => [
      ...current,
      { item, quantity: parsedQuantity, available },
    ]);
    setBarcode("");
    setQuantity("1");
    setCreateError(null);
    createAttempt.current = null;
    requestAnimationFrame(() => barcodeInput.current?.focus());
  }

  function updateDraftQuantity(sku: string, value: string) {
    const parsed = Number(value);
    setDraftLines((current) =>
      current.map((line) =>
        line.item.sku === sku
          ? {
              ...line,
              quantity:
                Number.isInteger(parsed) && parsed >= 0
                  ? parsed
                  : line.quantity,
            }
          : line,
      ),
    );
    setCreateError(null);
    createAttempt.current = null;
  }

  function removeDraftLine(sku: string) {
    setDraftLines((current) =>
      current.filter((line) => line.item.sku !== sku),
    );
    setCreateError(null);
    createAttempt.current = null;
  }

  async function submitTransfer(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (creating) return;

    if (!sourceCode || !destinationCode) {
      setCreateError("Choose the source and destination.");
      return;
    }
    if (sourceCode === destinationCode) {
      setCreateError("Choose a different destination.");
      return;
    }
    if (!draftLines.length) {
      setCreateError("Add at least one product.");
      return;
    }
    if (
      draftLines.some(
        (line) =>
          !Number.isInteger(line.quantity) ||
          line.quantity <= 0 ||
          line.quantity > line.available,
      )
    ) {
      setCreateError("Review the quantities.");
      return;
    }

    const input = {
      source_location_code: sourceCode,
      destination_location_code: destinationCode,
      lines: draftLines.map((line) => ({
        barcode: line.item.barcode,
        quantity: line.quantity,
      })),
      ...(note.trim() ? { note: note.trim() } : {}),
    };
    const key = attemptKey(
      createAttempt,
      JSON.stringify(input),
      "transfer",
    );

    setCreating(true);
    setCreateError(null);
    try {
      const response = await stockpileApi.createTransfer(token, key, input);
      createAttempt.current = null;
      setCreateOpen(false);
      resetCreate();
      await loadTransfers();
      setNotice("Handoff " + transferCode(response.data.id) + " created.");
    } catch (error) {
      setCreateError(
        error instanceof Error ? error.message : "Transfer could not be created.",
      );
    } finally {
      setCreating(false);
    }
  }

  function openReceipt(transfer: InventoryTransfer) {
    setNotice(null);
    setReceivingTransfer(transfer);
    setReceiptLines(
      transfer.lines.map((line) => ({
        barcode: line.barcode,
        quantity: String(line.quantity_sent),
        note: "",
      })),
    );
    setReceiverBadge("");
    setReceiverPin("");
    setReceiveNote("");
    setReceiveError(null);
    receiveAttempt.current = null;
    requestAnimationFrame(() => badgeInput.current?.focus());
  }

  function closeReceipt() {
    if (receiving) return;
    setReceivingTransfer(null);
    setReceiptLines([]);
    setReceiverBadge("");
    setReceiverPin("");
    setReceiveNote("");
    setReceiveError(null);
    receiveAttempt.current = null;
  }

  function updateReceiptLine(
    barcodeValue: string,
    change: Partial<ReceiptLineDraft>,
  ) {
    setReceiptLines((current) =>
      current.map((line) =>
        line.barcode === barcodeValue ? { ...line, ...change } : line,
      ),
    );
    setReceiveError(null);
    receiveAttempt.current = null;
  }

  async function submitReceipt(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!receivingTransfer || receiving) return;

    const badge = receiverBadge.trim();
    if (!badge) {
      setReceiveError("Scan the receiver badge.");
      return;
    }

    const sentByBarcode = new Map(
      receivingTransfer.lines.map((line) => [
        line.barcode,
        line.quantity_sent,
      ]),
    );
    const parsedLines = receiptLines.map((line) => ({
      ...line,
      parsedQuantity: Number(line.quantity),
      sent: sentByBarcode.get(line.barcode) ?? 0,
    }));
    if (
      parsedLines.some(
        (line) =>
          !Number.isInteger(line.parsedQuantity) ||
          line.parsedQuantity < 0 ||
          line.parsedQuantity > 1_000_000,
      )
    ) {
      setReceiveError("Review the received quantities.");
      return;
    }
    if (
      parsedLines.some(
        (line) =>
          line.parsedQuantity !== line.sent && !line.note.trim(),
      )
    ) {
      setReceiveError("Add a note beside each count difference.");
      return;
    }

    const discrepancies = parsedLines
      .filter(
        (line) =>
          line.parsedQuantity !== line.sent || Boolean(line.note.trim()),
      )
      .map((line) => ({
        barcode: line.barcode,
        quantity_received: line.parsedQuantity,
        ...(line.note.trim() ? { note: line.note.trim() } : {}),
      }));
    const input = {
      receiver_badge_code: badge,
      ...(receiverPin ? { receiver_pin: receiverPin } : {}),
      ...(discrepancies.length ? { discrepancies } : {}),
      ...(receiveNote.trim() ? { note: receiveNote.trim() } : {}),
    };
    const signature = JSON.stringify({
      transfer: receivingTransfer.id,
      ...input,
    });
    const key = attemptKey(receiveAttempt, signature, "receive");

    setReceiving(true);
    setReceiveError(null);
    try {
      const response = await stockpileApi.receiveTransfer(
        token,
        receivingTransfer.id,
        key,
        input,
      );
      receiveAttempt.current = null;
      setReceivingTransfer(null);
      setReceiptLines([]);
      setReceiverBadge("");
      setReceiverPin("");
      setReceiveNote("");
      await Promise.all([loadTransfers(), onChanged()]);
      setNotice(
        response.data.has_discrepancy
          ? "Handoff " +
              transferCode(response.data.id) +
              " received · check count."
          : "Handoff " + transferCode(response.data.id) + " received.",
      );
    } catch (error) {
      setReceiveError(
        error instanceof Error ? error.message : "Receipt could not be confirmed.",
      );
    } finally {
      setReceiving(false);
    }
  }

  return (
    <section
      className={(embedded ? "ledger-panel" : "panel") + " transfer-ledger"}
      aria-label="Stock transfers"
    >
      <div className="ledger-toolbar transfer-toolbar">
        <button
          type="button"
          className="button button-primary"
          onClick={openCreate}
          disabled={!sourceLocations.length || !destinationLocations.length}
        >
          New handoff
        </button>
      </div>

      {notice && (
        <div className="transfer-notice" role="status">
          <span>{notice}</span>
          <button
            type="button"
            className="text-button"
            onClick={() => setNotice(null)}
          >
            Dismiss
          </button>
        </div>
      )}

      {loading ? (
        <div className="loading-state panel-state" role="status">
          <span className="spinner" aria-hidden="true" />
          Loading…
        </div>
      ) : loadError ? (
        <div className="recovery-state panel-state" role="alert">
          <div>
            <strong>Transfers unavailable</strong>
            <span>{loadError}</span>
          </div>
          <button
            type="button"
            className="button button-secondary"
            onClick={() => void loadTransfers()}
          >
            Retry
          </button>
        </div>
      ) : (
        <div className="transfer-register">
          <section className="transfer-queue" aria-labelledby="pending-title">
            <header className="transfer-section-head">
              <h2 id="pending-title">Pending</h2>
              <span>{pendingTransfers.length.toLocaleString("en-PH")}</span>
            </header>

            {pendingTransfers.length ? (
              <div className="transfer-rows">
                {pendingTransfers.map((transfer) => (
                  <article className="transfer-row" key={transfer.id}>
                    <div className="transfer-code">
                      <span>Handoff</span>
                      <strong>{transferCode(transfer.id)}</strong>
                    </div>
                    <div className="transfer-route">
                      <span>{transfer.source_location_code}</span>
                      <i aria-hidden="true">→</i>
                      <span>{transfer.destination_location_code}</span>
                    </div>
                    <div className="transfer-line-summary">
                      <strong>
                        {transfer.lines.length.toLocaleString("en-PH")}{" "}
                        {transfer.lines.length === 1 ? "item" : "items"}
                      </strong>
                      <span>
                        {transfer.initiated_by_name} ·{" "}
                        {formatTime(transfer.created_at)}
                      </span>
                    </div>
                    <button
                      type="button"
                      className="button button-primary"
                      onClick={() => openReceipt(transfer)}
                    >
                      Receive
                    </button>
                  </article>
                ))}
              </div>
            ) : (
              <div className="empty-state compact">
                <strong>No pending handoffs.</strong>
              </div>
            )}
          </section>

          <section className="transfer-recent" aria-labelledby="recent-title">
            <header className="transfer-section-head">
              <h2 id="recent-title">Recent</h2>
            </header>

            {recentTransfers.length ? (
              <div className="transfer-rows">
                {recentTransfers.map((transfer) => (
                  <article
                    className="transfer-row transfer-row-complete"
                    key={transfer.id}
                  >
                    <div className="transfer-code">
                      <span>Handoff</span>
                      <strong>{transferCode(transfer.id)}</strong>
                    </div>
                    <div className="transfer-route">
                      <span>{transfer.source_location_code}</span>
                      <i aria-hidden="true">→</i>
                      <span>{transfer.destination_location_code}</span>
                    </div>
                    <div className="transfer-line-summary">
                      <strong>{transfer.received_by_name || "—"}</strong>
                      <span>
                        {transfer.received_at
                          ? formatTime(transfer.received_at)
                          : formatTime(transfer.created_at)}
                      </span>
                    </div>
                    <span className={"status status-" + transfer.status}>
                      {statusLabel(transfer.status)}
                    </span>
                  </article>
                ))}
              </div>
            ) : (
              <div className="empty-state compact">
                <strong>No completed handoffs.</strong>
              </div>
            )}
          </section>
        </div>
      )}

      {createOpen && (
        <div
          className="dialog-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeCreate();
          }}
        >
          <section
            className="product-editor transfer-editor"
            role="dialog"
            aria-modal="true"
            aria-labelledby="transfer-create-title"
          >
            <header>
              <div>
                <span className="eyebrow">Stock handoff</span>
                <h3 id="transfer-create-title">New transfer</h3>
              </div>
              <button
                type="button"
                className="dialog-close"
                onClick={closeCreate}
                disabled={creating}
                aria-label="Close"
              >
                ×
              </button>
            </header>

            <form className="transfer-editor-form" onSubmit={submitTransfer}>
              <div className="transfer-location-grid">
                <div>
                  <label htmlFor="transfer-source">From</label>
                  <select
                    id="transfer-source"
                    value={sourceCode}
                    onChange={(event) => {
                      setSourceCode(event.target.value);
                      setDestinationCode((current) =>
                        current === event.target.value ? "" : current,
                      );
                      setCreateError(null);
                      createAttempt.current = null;
                    }}
                    disabled={creating || draftLines.length > 0}
                  >
                    <option value="">Select location</option>
                    {sourceLocations.map((location) => (
                      <option key={location.code} value={location.code}>
                        {locationLabel(location)}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label htmlFor="transfer-destination">To</label>
                  <select
                    id="transfer-destination"
                    value={destinationCode}
                    onChange={(event) => {
                      setDestinationCode(event.target.value);
                      setCreateError(null);
                      createAttempt.current = null;
                    }}
                    disabled={creating || !sourceCode}
                  >
                    <option value="">Select location</option>
                    {destinationLocations.map((location) => (
                      <option key={location.code} value={location.code}>
                        {locationLabel(location)}
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              <div className="transfer-scan-row">
                <div>
                  <label htmlFor="transfer-barcode">Barcode or SKU</label>
                  <input
                    ref={barcodeInput}
                    id="transfer-barcode"
                    value={barcode}
                    onChange={(event) => {
                      setBarcode(event.target.value);
                      setCreateError(null);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                        addLine();
                      }
                    }}
                    disabled={creating || !sourceCode}
                    autoComplete="off"
                  />
                </div>
                <div>
                  <label htmlFor="transfer-quantity">Qty</label>
                  <input
                    id="transfer-quantity"
                    type="number"
                    min="1"
                    max="1000000"
                    step="1"
                    inputMode="numeric"
                    value={quantity}
                    onChange={(event) => {
                      setQuantity(event.target.value);
                      setCreateError(null);
                    }}
                    disabled={creating || !sourceCode}
                  />
                </div>
                <button
                  type="button"
                  className="button button-secondary"
                  onClick={addLine}
                  disabled={creating || !sourceCode}
                >
                  Add
                </button>
              </div>

              <div className="transfer-draft">
                {draftLines.length ? (
                  draftLines.map((line) => (
                    <div className="transfer-draft-line" key={line.item.sku}>
                      <span className="product-mark" aria-hidden="true">
                        {productMark(line.item)}
                      </span>
                      <div>
                        <strong>{line.item.product_name}</strong>
                        <span>
                          {line.item.sku} ·{" "}
                          {line.available.toLocaleString("en-PH")} available
                        </span>
                      </div>
                      <input
                        aria-label={"Quantity for " + line.item.product_name}
                        type="number"
                        min="1"
                        max={line.available}
                        step="1"
                        value={line.quantity}
                        onChange={(event) =>
                          updateDraftQuantity(line.item.sku, event.target.value)
                        }
                        disabled={creating}
                      />
                      <button
                        type="button"
                        className="text-button danger-text"
                        onClick={() => removeDraftLine(line.item.sku)}
                        disabled={creating}
                      >
                        Remove
                      </button>
                    </div>
                  ))
                ) : (
                  <div className="transfer-draft-empty">Scan the first item.</div>
                )}
              </div>

              <div>
                <label htmlFor="transfer-note">Note</label>
                <input
                  id="transfer-note"
                  value={note}
                  onChange={(event) => {
                    setNote(event.target.value);
                    setCreateError(null);
                    createAttempt.current = null;
                  }}
                  maxLength={500}
                  disabled={creating}
                />
              </div>

              {createError && (
                <div className="alert alert-error" role="alert">
                  {createError}
                </div>
              )}

              <footer>
                <button
                  type="button"
                  className="text-button"
                  onClick={() => {
                    resetCreate();
                    requestAnimationFrame(() => barcodeInput.current?.focus());
                  }}
                  disabled={creating}
                >
                  Clear
                </button>
                <div className="button-row">
                  <button
                    type="button"
                    className="button button-secondary"
                    onClick={closeCreate}
                    disabled={creating}
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    className="button button-primary"
                    disabled={creating || !draftLines.length}
                  >
                    {creating ? "Creating…" : "Create handoff"}
                  </button>
                </div>
              </footer>
            </form>
          </section>
        </div>
      )}

      {receivingTransfer && (
        <div
          className="dialog-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeReceipt();
          }}
        >
          <section
            className="product-editor transfer-editor transfer-receive-editor"
            role="dialog"
            aria-modal="true"
            aria-labelledby="transfer-receive-title"
          >
            <header>
              <div>
                <span className="eyebrow">
                  Handoff {transferCode(receivingTransfer.id)}
                </span>
                <h3 id="transfer-receive-title">
                  {receivingTransfer.destination_location_code}
                </h3>
              </div>
              <button
                type="button"
                className="dialog-close"
                onClick={closeReceipt}
                disabled={receiving}
                aria-label="Close"
              >
                ×
              </button>
            </header>

            <form className="transfer-editor-form" onSubmit={submitReceipt}>
              <div className="transfer-receipt-route">
                <span>{receivingTransfer.source_location_path}</span>
                <i aria-hidden="true">→</i>
                <span>{receivingTransfer.destination_location_path}</span>
              </div>

              <div className="transfer-receipt-lines">
                {receivingTransfer.lines.map((line) => {
                  const draft = receiptLines.find(
                    (entry) => entry.barcode === line.barcode,
                  );
                  const quantityId = "received-" + line.sku;
                  const noteId = "difference-" + line.sku;
                  return (
                    <div className="transfer-receipt-line" key={line.sku}>
                      <span className="product-mark" aria-hidden="true">
                        {productMark({
                          product_name: line.product_name,
                          variant: null,
                        })}
                      </span>
                      <div>
                        <strong>{line.product_name}</strong>
                        <span>{line.sku}</span>
                      </div>
                      <div>
                        <label htmlFor={quantityId}>Received</label>
                        <input
                          id={quantityId}
                          type="number"
                          min="0"
                          max="1000000"
                          step="1"
                          inputMode="numeric"
                          value={draft?.quantity ?? String(line.quantity_sent)}
                          onChange={(event) =>
                            updateReceiptLine(line.barcode, {
                              quantity: event.target.value,
                            })
                          }
                          disabled={receiving}
                        />
                        <small>
                          of {line.quantity_sent.toLocaleString("en-PH")}
                        </small>
                      </div>
                      <div>
                        <label htmlFor={noteId}>Note</label>
                        <input
                          id={noteId}
                          value={draft?.note ?? ""}
                          onChange={(event) =>
                            updateReceiptLine(line.barcode, {
                              note: event.target.value,
                            })
                          }
                          maxLength={240}
                          disabled={receiving}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>

              <div className="receiver-signoff">
                <div>
                  <label htmlFor="receiver-badge">Receiver badge</label>
                  <input
                    ref={badgeInput}
                    id="receiver-badge"
                    value={receiverBadge}
                    onChange={(event) => {
                      setReceiverBadge(event.target.value);
                      setReceiveError(null);
                      receiveAttempt.current = null;
                    }}
                    autoComplete="off"
                    disabled={receiving}
                  />
                </div>
                <div>
                  <label htmlFor="receiver-pin">PIN</label>
                  <input
                    id="receiver-pin"
                    type="password"
                    inputMode="numeric"
                    pattern="[0-9]*"
                    value={receiverPin}
                    onChange={(event) => {
                      setReceiverPin(event.target.value);
                      setReceiveError(null);
                      receiveAttempt.current = null;
                    }}
                    minLength={4}
                    maxLength={12}
                    autoComplete="off"
                    disabled={receiving}
                  />
                </div>
              </div>

              <div>
                <label htmlFor="receive-note">Note</label>
                <input
                  id="receive-note"
                  value={receiveNote}
                  onChange={(event) => {
                    setReceiveNote(event.target.value);
                    setReceiveError(null);
                    receiveAttempt.current = null;
                  }}
                  maxLength={500}
                  disabled={receiving}
                />
              </div>

              {receiveError && (
                <div className="alert alert-error" role="alert">
                  {receiveError}
                </div>
              )}

              <footer>
                <span className="transfer-sender">
                  Sent by {receivingTransfer.initiated_by_name}
                </span>
                <div className="button-row">
                  <button
                    type="button"
                    className="button button-secondary"
                    onClick={closeReceipt}
                    disabled={receiving}
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    className="button button-primary"
                    disabled={receiving}
                  >
                    {receiving ? "Confirming…" : "Confirm receipt"}
                  </button>
                </div>
              </footer>
            </form>
          </section>
        </div>
      )}
    </section>
  );
}
