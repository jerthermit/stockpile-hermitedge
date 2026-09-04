"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type {
  FormEvent,
  KeyboardEvent as ReactKeyboardEvent,
  ReactNode,
} from "react";
import { stockpileApi } from "../lib/api";
import TransferPanel from "./TransferPanel";
import type {
  DamageCase,
  DamageCategory,
  DamageResolution,
  InventoryDelivery,
  InventoryItem,
  InventoryInvestigation,
  InventoryReservation,
  InventoryReturn,
  InventoryStockCount,
  InvestigationKind,
  InvestigationResolution,
  ReservationAction,
  ReturnType,
  Role,
  StockCondition,
  StockLocation,
} from "../lib/types";

interface StockControlPanelProps {
  token: string;
  role: Role;
  items: InventoryItem[];
  locations: StockLocation[];
  onChanged: () => void | Promise<void>;
  embedded?: boolean;
}

type StockControlView =
  | "receive"
  | "move"
  | "orders"
  | "issues"
  | "count";

type StockRecordView =
  | "deliveries"
  | "returns"
  | "damage"
  | "investigations"
  | "reservations"
  | "counts";

type IssueView = "investigations" | "damage" | "returns";

interface RetryAttempt {
  signature: string;
  key: string;
}

interface DraftLine {
  item: InventoryItem;
  locationCode: string;
  quantity: number;
  condition: StockCondition;
}

interface CountDraftLine {
  item: InventoryItem;
  expected: number;
  quantity: string;
}

type DialogState =
  | { kind: "delivery" }
  | { kind: "return" }
  | { kind: "damage" }
  | { kind: "investigation" }
  | { kind: "reservation" }
  | { kind: "count" }
  | { kind: "damage-resolution"; record: DamageCase }
  | {
      kind: "investigation-review";
      record: InventoryInvestigation;
    }
  | {
      kind: "investigation-resolution";
      record: InventoryInvestigation;
    }
  | {
      kind: "reservation-close";
      record: InventoryReservation;
      action: ReservationAction;
    }
  | {
      kind: "count-close";
      record: InventoryStockCount;
      action: "approve" | "cancel";
    }
  | null;

const UNASSIGNED = "UNASSIGNED";

const DAMAGE_CATEGORIES: Array<{
  value: DamageCategory;
  label: string;
}> = [
  { value: "physical_damage", label: "Physical damage" },
  { value: "quality_issue", label: "Quality issue" },
  { value: "expired", label: "Expired" },
  { value: "water_damage", label: "Water damage" },
  { value: "other", label: "Other" },
];

const DAMAGE_RESOLUTIONS: Array<{
  value: DamageResolution;
  label: string;
}> = [
  { value: "restock", label: "Return to stock" },
  { value: "write_off", label: "Write off" },
  { value: "return_to_supplier", label: "Return to supplier" },
];

const INVESTIGATION_KINDS: Array<{
  value: InvestigationKind;
  label: string;
}> = [
  { value: "missing", label: "Missing" },
  { value: "misplaced", label: "Misplaced" },
  { value: "unexpected", label: "Unexpected" },
];

const INVESTIGATION_RESOLUTIONS: Array<{
  value: InvestigationResolution;
  label: string;
}> = [
  { value: "located", label: "Located" },
  { value: "confirmed_missing", label: "Confirm missing" },
  { value: "record_error", label: "Record error" },
  { value: "dismissed", label: "Dismiss" },
];

function newSubmissionKey(prefix: string) {
  const suffix =
    globalThis.crypto?.randomUUID?.() ??
    String(Date.now()) + "-" + Math.random().toString(16).slice(2);
  return (prefix + "-" + suffix).slice(0, 128);
}

function attemptKey(
  attempt: { current: RetryAttempt | null },
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
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-PH", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Manila",
  }).format(date);
}

function recordCode(id: string) {
  return id.slice(0, 8).toUpperCase();
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

function findItem(items: InventoryItem[], value: string) {
  const code = value.trim().toLowerCase();
  if (!code) return null;
  return (
    items.find(
      (item) =>
        item.active !== false &&
        (item.barcode.toLowerCase() === code ||
          item.sku.toLowerCase() === code),
    ) ?? null
  );
}

function positionAt(item: InventoryItem, locationCode: string) {
  return (
    item.positions?.find(
      (position) => position.location_code === locationCode,
    ) ?? null
  );
}

function availableAt(item: InventoryItem, locationCode: string) {
  const position = positionAt(item, locationCode);
  if (!position) return 0;
  return Math.max(
    0,
    position.available_quantity ??
      position.quantity -
        (position.reserved_quantity ?? 0) -
        (position.damaged_quantity ?? 0),
  );
}

function damagedAt(item: InventoryItem, locationCode: string) {
  return Math.max(
    0,
    positionAt(item, locationCode)?.damaged_quantity ?? 0,
  );
}

function quantityAt(item: InventoryItem, locationCode: string) {
  return Math.max(0, positionAt(item, locationCode)?.quantity ?? 0);
}

function totalLines(lines: Array<{ quantity: number }>) {
  return lines.reduce((sum, line) => sum + line.quantity, 0);
}

function errorText(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function validBadge(value: string) {
  const length = value.trim().length;
  return length >= 4 && length <= 128;
}

function validPin(value: string) {
  return !value || /^\d{4,12}$/.test(value);
}

function validReason(value: string) {
  const length = value.trim().length;
  return length >= 3 && length <= 500;
}

interface DialogShellProps {
  id: string;
  eyebrow: string;
  title: string;
  busy: boolean;
  onClose: () => void;
  wide?: boolean;
  children: ReactNode;
}

function DialogShell({
  id,
  eyebrow,
  title,
  busy,
  onClose,
  wide = false,
  children,
}: DialogShellProps) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  const busyRef = useRef(busy);
  closeRef.current = onClose;
  busyRef.current = busy;

  useEffect(() => {
    const previousFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const frame = requestAnimationFrame(() => {
      const dialog = dialogRef.current;
      if (!dialog) return;
      const preferred = dialog.querySelector<HTMLElement>("[data-autofocus]");
      const first = dialog.querySelector<HTMLElement>(
        "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])",
      );
      (preferred ?? first ?? dialog).focus();
    });

    function handleKeyDown(event: KeyboardEvent) {
      const dialog = dialogRef.current;
      if (!dialog) return;
      if (event.key === "Escape") {
        if (!busyRef.current) closeRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        dialog.querySelectorAll<HTMLElement>(
          "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])",
        ),
      );
      if (!focusable.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, []);

  return (
    <div
      className="dialog-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        ref={dialogRef}
        className={
          "product-editor stock-control-dialog" +
          (wide ? " stock-control-dialog-wide" : "")
        }
        role="dialog"
        aria-modal="true"
        aria-labelledby={id}
        aria-busy={busy}
        tabIndex={-1}
      >
        <header>
          <div>
            <span className="eyebrow">{eyebrow}</span>
            <h3 id={id}>{title}</h3>
          </div>
          <button
            type="button"
            className="dialog-close"
            onClick={onClose}
            disabled={busy}
            aria-label="Close"
          >
            ×
          </button>
        </header>
        {children}
      </section>
    </div>
  );
}

interface LineBuilderProps {
  items: InventoryItem[];
  locations: StockLocation[];
  lines: DraftLine[];
  onChange: (lines: DraftLine[]) => void;
  disabled: boolean;
  allowCondition?: boolean;
  limitFor?: (
    item: InventoryItem,
    locationCode: string,
    condition: StockCondition,
  ) => number | null;
}

function LineBuilder({
  items,
  locations,
  lines,
  onChange,
  disabled,
  allowCondition = false,
  limitFor,
}: LineBuilderProps) {
  const [barcode, setBarcode] = useState("");
  const [locationCode, setLocationCode] = useState("");
  const [quantity, setQuantity] = useState("1");
  const [condition, setCondition] = useState<StockCondition>("sellable");
  const [error, setError] = useState<string | null>(null);
  const barcodeRef = useRef<HTMLInputElement>(null);

  function addLine() {
    setError(null);
    const item = findItem(items, barcode);
    const parsed = Number(quantity);
    if (!item) {
      setError("Product not found.");
      return;
    }
    if (!locationCode) {
      setError("Choose a location.");
      return;
    }
    if (!Number.isInteger(parsed) || parsed <= 0 || parsed > 1_000_000) {
      setError("Enter a valid whole quantity.");
      return;
    }

    const effectiveCondition = allowCondition ? condition : "sellable";
    const existingIndex = lines.findIndex(
      (line) =>
        line.item.sku === item.sku &&
        line.locationCode === locationCode &&
        line.condition === effectiveCondition,
    );
    const existingQuantity =
      existingIndex >= 0 ? lines[existingIndex].quantity : 0;
    const limit = limitFor?.(item, locationCode, effectiveCondition) ?? null;
    if (limit !== null && existingQuantity + parsed > limit) {
      setError(`Only ${limit.toLocaleString("en-PH")} available.`);
      return;
    }

    if (existingIndex >= 0) {
      onChange(
        lines.map((line, index) =>
          index === existingIndex
            ? { ...line, quantity: line.quantity + parsed }
            : line,
        ),
      );
    } else {
      onChange([
        ...lines,
        {
          item,
          locationCode,
          quantity: parsed,
          condition: effectiveCondition,
        },
      ]);
    }
    setBarcode("");
    setQuantity("1");
    requestAnimationFrame(() => barcodeRef.current?.focus());
  }

  function updateQuantity(index: number, value: string) {
    const parsed = Number(value);
    if (!Number.isInteger(parsed) || parsed < 0) return;
    onChange(
      lines.map((line, lineIndex) =>
        lineIndex === index ? { ...line, quantity: parsed } : line,
      ),
    );
    setError(null);
  }

  function updateCondition(index: number, value: StockCondition) {
    onChange(
      lines.map((line, lineIndex) =>
        lineIndex === index ? { ...line, condition: value } : line,
      ),
    );
    setError(null);
  }

  return (
    <div className="stock-line-builder">
      <div className="stock-scan-row">
        <div>
          <label htmlFor="stock-line-barcode">Barcode or SKU</label>
          <input
            ref={barcodeRef}
            id="stock-line-barcode"
            data-autofocus
            value={barcode}
            onChange={(event) => {
              setBarcode(event.target.value);
              setError(null);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                addLine();
              }
            }}
            autoComplete="off"
            disabled={disabled}
          />
        </div>
        <div>
          <label htmlFor="stock-line-location">Location</label>
          <select
            id="stock-line-location"
            value={locationCode}
            onChange={(event) => {
              setLocationCode(event.target.value);
              setError(null);
            }}
            disabled={disabled}
          >
            <option value="">Select location</option>
            {locations.map((location) => (
              <option key={location.code} value={location.code}>
                {locationLabel(location)}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="stock-line-quantity">Qty</label>
          <input
            id="stock-line-quantity"
            type="number"
            min="1"
            max="1000000"
            step="1"
            inputMode="numeric"
            value={quantity}
            onChange={(event) => {
              setQuantity(event.target.value);
              setError(null);
            }}
            disabled={disabled}
          />
        </div>
        {allowCondition && (
          <div>
            <label htmlFor="stock-line-condition">Condition</label>
            <select
              id="stock-line-condition"
              value={condition}
              onChange={(event) =>
                setCondition(event.target.value as StockCondition)
              }
              disabled={disabled}
            >
              <option value="sellable">Sellable</option>
              <option value="damaged">Damaged</option>
            </select>
          </div>
        )}
        <button
          type="button"
          className="button button-secondary"
          onClick={addLine}
          disabled={disabled}
        >
          Add
        </button>
      </div>

      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}

      <div className="stock-draft-lines" aria-label="Selected products">
        {lines.length ? (
          lines.map((line, index) => (
            <div
              className="stock-draft-line"
              key={`${line.item.sku}-${line.locationCode}-${line.condition}`}
            >
              <span className="product-mark" aria-hidden="true">
                {productMark(line.item)}
              </span>
              <div className="stock-draft-identity">
                <strong>{line.item.product_name}</strong>
                <span>
                  {line.item.sku} · {line.locationCode}
                </span>
              </div>
              <input
                aria-label={`Quantity for ${line.item.product_name}`}
                type="number"
                min="1"
                max="1000000"
                step="1"
                value={line.quantity}
                onChange={(event) =>
                  updateQuantity(index, event.target.value)
                }
                disabled={disabled}
              />
              {allowCondition && (
                <select
                  aria-label={`Condition for ${line.item.product_name}`}
                  value={line.condition}
                  onChange={(event) =>
                    updateCondition(
                      index,
                      event.target.value as StockCondition,
                    )
                  }
                  disabled={disabled}
                >
                  <option value="sellable">Sellable</option>
                  <option value="damaged">Damaged</option>
                </select>
              )}
              <button
                type="button"
                className="text-button danger-text"
                onClick={() =>
                  onChange(lines.filter((_, lineIndex) => lineIndex !== index))
                }
                disabled={disabled}
              >
                Remove
              </button>
            </div>
          ))
        ) : (
          <div className="stock-draft-empty">Scan the first item.</div>
        )}
      </div>
    </div>
  );
}

function canonicalLines(lines: DraftLine[]) {
  return [...lines].sort((left, right) =>
    [left.item.barcode, left.locationCode, left.condition]
      .join("\u0000")
      .localeCompare(
        [right.item.barcode, right.locationCode, right.condition].join(
          "\u0000",
        ),
      ),
  );
}

function validLines(
  lines: DraftLine[],
  limitFor?: (
    item: InventoryItem,
    locationCode: string,
    condition: StockCondition,
  ) => number | null,
) {
  if (!lines.length || lines.length > 500) return false;
  const seen = new Set<string>();
  for (const line of lines) {
    const key = [line.item.barcode, line.locationCode, line.condition].join(
      "\u0000",
    );
    if (seen.has(key)) return false;
    seen.add(key);
    if (
      !Number.isInteger(line.quantity) ||
      line.quantity <= 0 ||
      line.quantity > 1_000_000
    ) {
      return false;
    }
    const limit = limitFor?.(
      line.item,
      line.locationCode,
      line.condition,
    );
    if (limit !== undefined && limit !== null && line.quantity > limit) {
      return false;
    }
  }
  return true;
}

interface BadgeFieldsProps {
  prefix: string;
  label: string;
  badge: string;
  pin: string;
  onBadge: (value: string) => void;
  onPin: (value: string) => void;
  disabled: boolean;
}

function BadgeFields({
  prefix,
  label,
  badge,
  pin,
  onBadge,
  onPin,
  disabled,
}: BadgeFieldsProps) {
  return (
    <div className="stock-signoff-grid">
      <div>
        <label htmlFor={`${prefix}-badge`}>{label}</label>
        <input
          id={`${prefix}-badge`}
          value={badge}
          onChange={(event) => onBadge(event.target.value)}
          minLength={4}
          maxLength={128}
          autoComplete="off"
          disabled={disabled}
        />
      </div>
      <div>
        <label htmlFor={`${prefix}-pin`}>PIN</label>
        <input
          id={`${prefix}-pin`}
          type="password"
          inputMode="numeric"
          pattern="[0-9]*"
          minLength={4}
          maxLength={12}
          value={pin}
          onChange={(event) => onPin(event.target.value)}
          autoComplete="off"
          disabled={disabled}
        />
      </div>
    </div>
  );
}

interface WorkflowFormProps {
  token: string;
  items: InventoryItem[];
  locations: StockLocation[];
  onClose: () => void;
  onSaved: (message: string) => void;
}

function DeliveryForm({
  token,
  items,
  locations,
  onClose,
  onSaved,
}: WorkflowFormProps) {
  const [lines, setLines] = useState<DraftLine[]>([]);
  const [supplier, setSupplier] = useState("");
  const [reference, setReference] = useState("");
  const [badge, setBadge] = useState("");
  const [pin, setPin] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const attempt = useRef<RetryAttempt | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    if (!validLines(lines)) {
      setError("Review the delivery lines.");
      return;
    }
    if (!validBadge(badge)) {
      setError("Scan the receiver badge.");
      return;
    }
    if (!validPin(pin)) {
      setError("Enter a valid numeric PIN.");
      return;
    }
    const input = {
      ...(supplier.trim() ? { supplier_name: supplier.trim() } : {}),
      ...(reference.trim() ? { reference: reference.trim() } : {}),
      source: "scanner" as const,
      receiver_badge_code: badge.trim(),
      ...(pin ? { receiver_pin: pin } : {}),
      ...(note.trim() ? { note: note.trim() } : {}),
      lines: canonicalLines(lines).map((line) => ({
        barcode: line.item.barcode,
        quantity: line.quantity,
        location_code: line.locationCode,
      })),
    };
    const key = attemptKey(
      attempt,
      JSON.stringify(input),
      "delivery",
    );
    setSaving(true);
    setError(null);
    try {
      const response = await stockpileApi.createDelivery(token, key, input);
      attempt.current = null;
      onSaved(
        `Delivery ${recordCode(response.data.id)} ${
          response.replayed ? "was already received" : "received"
        }.`,
      );
    } catch (submissionError) {
      setError(errorText(submissionError, "Delivery could not be saved."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DialogShell
      id="delivery-dialog-title"
      eyebrow="Delivery"
      title="Receive stock"
      busy={saving}
      onClose={onClose}
      wide
    >
      <form className="stock-control-form" onSubmit={submit} noValidate>
        <div className="stock-form-grid">
          <div>
            <label htmlFor="delivery-supplier">Supplier</label>
            <input
              id="delivery-supplier"
              value={supplier}
              onChange={(event) => setSupplier(event.target.value)}
              maxLength={160}
              disabled={saving}
            />
          </div>
          <div>
            <label htmlFor="delivery-reference">Reference</label>
            <input
              id="delivery-reference"
              value={reference}
              onChange={(event) => setReference(event.target.value)}
              maxLength={120}
              disabled={saving}
            />
          </div>
        </div>

        <LineBuilder
          items={items}
          locations={locations}
          lines={lines}
          onChange={(next) => {
            setLines(next);
            setError(null);
          }}
          disabled={saving}
        />

        <BadgeFields
          prefix="delivery-receiver"
          label="Receiver badge"
          badge={badge}
          pin={pin}
          onBadge={(value) => {
            setBadge(value);
            setError(null);
          }}
          onPin={(value) => {
            setPin(value);
            setError(null);
          }}
          disabled={saving}
        />

        <div>
          <label htmlFor="delivery-note">Note</label>
          <input
            id="delivery-note"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            maxLength={500}
            disabled={saving}
          />
        </div>

        {error && (
          <div className="alert alert-error" role="alert">
            {error}
          </div>
        )}

        <footer>
          <span className="stock-form-total">
            {totalLines(lines).toLocaleString("en-PH")} units
          </span>
          <div className="button-row">
            <button
              type="button"
              className="button button-secondary"
              onClick={onClose}
              disabled={saving}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="button button-primary"
              disabled={saving || !lines.length}
            >
              {saving ? "Receiving…" : "Receive"}
            </button>
          </div>
        </footer>
      </form>
    </DialogShell>
  );
}

function ReturnForm({
  token,
  items,
  locations,
  onClose,
  onSaved,
}: WorkflowFormProps) {
  const [returnType, setReturnType] =
    useState<ReturnType>("customer_return");
  const [lines, setLines] = useState<DraftLine[]>([]);
  const [party, setParty] = useState("");
  const [reference, setReference] = useState("");
  const [reason, setReason] = useState("");
  const [badge, setBadge] = useState("");
  const [pin, setPin] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const attempt = useRef<RetryAttempt | null>(null);

  const limitFor = useCallback(
    (
      item: InventoryItem,
      locationCode: string,
      condition: StockCondition,
    ) => {
      if (returnType === "customer_return") return null;
      return condition === "damaged"
        ? damagedAt(item, locationCode)
        : availableAt(item, locationCode);
    },
    [returnType],
  );

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    if (!validLines(lines, limitFor)) {
      setError("Review the return lines.");
      return;
    }
    if (!validReason(reason)) {
      setError("Enter the return reason.");
      return;
    }
    if (!validBadge(badge)) {
      setError("Scan the employee badge.");
      return;
    }
    if (!validPin(pin)) {
      setError("Enter a valid numeric PIN.");
      return;
    }
    const input = {
      return_type: returnType,
      ...(reference.trim() ? { reference: reference.trim() } : {}),
      ...(party.trim() ? { party_name: party.trim() } : {}),
      reason: reason.trim(),
      employee_badge_code: badge.trim(),
      ...(pin ? { employee_pin: pin } : {}),
      lines: canonicalLines(lines).map((line) => ({
        barcode: line.item.barcode,
        quantity: line.quantity,
        location_code: line.locationCode,
        condition: line.condition,
      })),
    };
    const key = attemptKey(attempt, JSON.stringify(input), "return");
    setSaving(true);
    setError(null);
    try {
      const response = await stockpileApi.createReturn(token, key, input);
      attempt.current = null;
      onSaved(
        `Return ${recordCode(response.data.id)} ${
          response.replayed ? "was already recorded" : "recorded"
        }.`,
      );
    } catch (submissionError) {
      setError(errorText(submissionError, "Return could not be saved."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DialogShell
      id="return-dialog-title"
      eyebrow="Return"
      title="Record return"
      busy={saving}
      onClose={onClose}
      wide
    >
      <form className="stock-control-form" onSubmit={submit} noValidate>
        <div className="stock-form-grid stock-form-grid-three">
          <div>
            <label htmlFor="return-type">Type</label>
            <select
              id="return-type"
              value={returnType}
              onChange={(event) => {
                setReturnType(event.target.value as ReturnType);
                setError(null);
              }}
              disabled={saving}
            >
              <option value="customer_return">Customer</option>
              <option value="supplier_return">Supplier</option>
            </select>
          </div>
          <div>
            <label htmlFor="return-party">
              {returnType === "customer_return" ? "Customer" : "Supplier"}
            </label>
            <input
              id="return-party"
              value={party}
              onChange={(event) => setParty(event.target.value)}
              maxLength={160}
              disabled={saving}
            />
          </div>
          <div>
            <label htmlFor="return-reference">Reference</label>
            <input
              id="return-reference"
              value={reference}
              onChange={(event) => setReference(event.target.value)}
              maxLength={120}
              disabled={saving}
            />
          </div>
        </div>

        <LineBuilder
          items={items}
          locations={locations}
          lines={lines}
          onChange={(next) => {
            setLines(next);
            setError(null);
          }}
          disabled={saving}
          allowCondition
          limitFor={limitFor}
        />

        <div>
          <label htmlFor="return-reason">Reason</label>
          <input
            id="return-reason"
            value={reason}
            onChange={(event) => {
              setReason(event.target.value);
              setError(null);
            }}
            maxLength={500}
            disabled={saving}
          />
        </div>

        <BadgeFields
          prefix="return-employee"
          label="Employee badge"
          badge={badge}
          pin={pin}
          onBadge={(value) => {
            setBadge(value);
            setError(null);
          }}
          onPin={(value) => {
            setPin(value);
            setError(null);
          }}
          disabled={saving}
        />

        {error && (
          <div className="alert alert-error" role="alert">
            {error}
          </div>
        )}

        <footer>
          <span className="stock-form-total">
            {totalLines(lines).toLocaleString("en-PH")} units
          </span>
          <div className="button-row">
            <button
              type="button"
              className="button button-secondary"
              onClick={onClose}
              disabled={saving}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="button button-primary"
              disabled={saving || !lines.length}
            >
              {saving ? "Recording…" : "Record"}
            </button>
          </div>
        </footer>
      </form>
    </DialogShell>
  );
}

function DamageForm({
  token,
  items,
  locations,
  onClose,
  onSaved,
}: WorkflowFormProps) {
  const [barcode, setBarcode] = useState("");
  const [locationCode, setLocationCode] = useState("");
  const [quantity, setQuantity] = useState("1");
  const [category, setCategory] =
    useState<DamageCategory>("physical_damage");
  const [reference, setReference] = useState("");
  const [reason, setReason] = useState("");
  const [badge, setBadge] = useState("");
  const [pin, setPin] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const attempt = useRef<RetryAttempt | null>(null);
  const item = findItem(items, barcode);
  const available = item ? availableAt(item, locationCode) : 0;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    const parsed = Number(quantity);
    if (!item) {
      setError("Product not found.");
      return;
    }
    if (!locationCode) {
      setError("Choose the exact location.");
      return;
    }
    if (
      !Number.isInteger(parsed) ||
      parsed <= 0 ||
      parsed > 1_000_000 ||
      parsed > available
    ) {
      setError(`Only ${available.toLocaleString("en-PH")} available.`);
      return;
    }
    if (!validReason(reason)) {
      setError("Enter the damage reason.");
      return;
    }
    if (!validBadge(badge)) {
      setError("Scan the employee badge.");
      return;
    }
    if (!validPin(pin)) {
      setError("Enter a valid numeric PIN.");
      return;
    }
    const input = {
      barcode: item.barcode,
      location_code: locationCode,
      quantity: parsed,
      category,
      ...(reference.trim() ? { reference: reference.trim() } : {}),
      employee_badge_code: badge.trim(),
      ...(pin ? { employee_pin: pin } : {}),
      reason: reason.trim(),
    };
    const key = attemptKey(attempt, JSON.stringify(input), "damage");
    setSaving(true);
    setError(null);
    try {
      const response = await stockpileApi.createDamage(token, key, input);
      attempt.current = null;
      onSaved(
        `Damage ${recordCode(response.data.id)} ${
          response.replayed ? "was already recorded" : "opened"
        }.`,
      );
    } catch (submissionError) {
      setError(errorText(submissionError, "Damage could not be recorded."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DialogShell
      id="damage-dialog-title"
      eyebrow="Damage"
      title="Report damage"
      busy={saving}
      onClose={onClose}
    >
      <form className="stock-control-form" onSubmit={submit} noValidate>
        <div>
          <label htmlFor="damage-barcode">Barcode or SKU</label>
          <input
            id="damage-barcode"
            data-autofocus
            value={barcode}
            onChange={(event) => {
              setBarcode(event.target.value);
              setError(null);
            }}
            autoComplete="off"
            disabled={saving}
          />
        </div>

        {item && (
          <div className="stock-resolved-item" aria-live="polite">
            <span className="product-mark" aria-hidden="true">
              {productMark(item)}
            </span>
            <div>
              <strong>{item.product_name}</strong>
              <span>{item.sku}</span>
            </div>
          </div>
        )}

        <div className="stock-form-grid stock-form-grid-three">
          <div>
            <label htmlFor="damage-location">Location</label>
            <select
              id="damage-location"
              value={locationCode}
              onChange={(event) => {
                setLocationCode(event.target.value);
                setError(null);
              }}
              disabled={saving}
            >
              <option value="">Select location</option>
              {locations.map((location) => (
                <option key={location.code} value={location.code}>
                  {locationLabel(location)}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="damage-quantity">Qty</label>
            <input
              id="damage-quantity"
              type="number"
              min="1"
              max={available || 1_000_000}
              step="1"
              inputMode="numeric"
              value={quantity}
              onChange={(event) => {
                setQuantity(event.target.value);
                setError(null);
              }}
              disabled={saving}
            />
          </div>
          <div>
            <label htmlFor="damage-category">Category</label>
            <select
              id="damage-category"
              value={category}
              onChange={(event) =>
                setCategory(event.target.value as DamageCategory)
              }
              disabled={saving}
            >
              {DAMAGE_CATEGORIES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="stock-form-grid">
          <div>
            <label htmlFor="damage-reference">Reference</label>
            <input
              id="damage-reference"
              value={reference}
              onChange={(event) => setReference(event.target.value)}
              maxLength={120}
              disabled={saving}
            />
          </div>
          <div>
            <label htmlFor="damage-reason">Reason</label>
            <input
              id="damage-reason"
              value={reason}
              onChange={(event) => {
                setReason(event.target.value);
                setError(null);
              }}
              maxLength={500}
              disabled={saving}
            />
          </div>
        </div>

        <BadgeFields
          prefix="damage-employee"
          label="Employee badge"
          badge={badge}
          pin={pin}
          onBadge={(value) => {
            setBadge(value);
            setError(null);
          }}
          onPin={(value) => {
            setPin(value);
            setError(null);
          }}
          disabled={saving}
        />

        {error && (
          <div className="alert alert-error" role="alert">
            {error}
          </div>
        )}

        <footer>
          <span className="stock-form-total">
            {locationCode
              ? `${available.toLocaleString("en-PH")} available`
              : ""}
          </span>
          <div className="button-row">
            <button
              type="button"
              className="button button-secondary"
              onClick={onClose}
              disabled={saving}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="button button-primary"
              disabled={saving}
            >
              {saving ? "Recording…" : "Record"}
            </button>
          </div>
        </footer>
      </form>
    </DialogShell>
  );
}

interface DamageResolutionFormProps {
  token: string;
  record: DamageCase;
  onClose: () => void;
  onSaved: (message: string) => void;
}

function DamageResolutionForm({
  token,
  record,
  onClose,
  onSaved,
}: DamageResolutionFormProps) {
  const [resolution, setResolution] =
    useState<DamageResolution>("restock");
  const [reason, setReason] = useState("");
  const [badge, setBadge] = useState("");
  const [pin, setPin] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const attempt = useRef<RetryAttempt | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    if (!validReason(reason)) {
      setError("Enter the resolution reason.");
      return;
    }
    if (!validBadge(badge)) {
      setError("Scan the employee badge.");
      return;
    }
    if (!validPin(pin)) {
      setError("Enter a valid numeric PIN.");
      return;
    }
    const input = {
      resolution,
      employee_badge_code: badge.trim(),
      ...(pin ? { employee_pin: pin } : {}),
      reason: reason.trim(),
    };
    const signature = JSON.stringify({ damage: record.id, ...input });
    const key = attemptKey(attempt, signature, "resolve-damage");
    setSaving(true);
    setError(null);
    try {
      const response = await stockpileApi.resolveDamage(
        token,
        record.id,
        key,
        input,
      );
      attempt.current = null;
      onSaved(
        `Damage ${recordCode(response.data.id)} ${
          response.replayed ? "was already resolved" : "resolved"
        }.`,
      );
    } catch (submissionError) {
      setError(errorText(submissionError, "Damage could not be resolved."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DialogShell
      id="damage-resolution-title"
      eyebrow={`Damage ${recordCode(record.id)}`}
      title="Resolve damage"
      busy={saving}
      onClose={onClose}
    >
      <form className="stock-control-form" onSubmit={submit} noValidate>
        <div className="stock-record-strip">
          <strong>{record.product_name}</strong>
          <span>
            {record.location_code} ·{" "}
            {record.remaining_quantity.toLocaleString("en-PH")} units
          </span>
        </div>

        <div>
          <label htmlFor="damage-resolution">Resolution</label>
          <select
            id="damage-resolution"
            data-autofocus
            value={resolution}
            onChange={(event) =>
              setResolution(event.target.value as DamageResolution)
            }
            disabled={saving}
          >
            {DAMAGE_RESOLUTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="damage-resolution-reason">Reason</label>
          <input
            id="damage-resolution-reason"
            value={reason}
            onChange={(event) => {
              setReason(event.target.value);
              setError(null);
            }}
            maxLength={500}
            disabled={saving}
          />
        </div>

        <BadgeFields
          prefix="damage-resolution-employee"
          label="Employee badge"
          badge={badge}
          pin={pin}
          onBadge={(value) => {
            setBadge(value);
            setError(null);
          }}
          onPin={(value) => {
            setPin(value);
            setError(null);
          }}
          disabled={saving}
        />

        {error && (
          <div className="alert alert-error" role="alert">
            {error}
          </div>
        )}

        <footer>
          <span />
          <div className="button-row">
            <button
              type="button"
              className="button button-secondary"
              onClick={onClose}
              disabled={saving}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="button button-primary"
              disabled={saving}
            >
              {saving ? "Resolving…" : "Resolve"}
            </button>
          </div>
        </footer>
      </form>
    </DialogShell>
  );
}

function investigationLocationSummary(record: InventoryInvestigation) {
  if (record.kind === "misplaced") {
    return `${record.expected_location_code ?? "—"} → ${
      record.observed_location_code ?? "—"
    }`;
  }

  if (record.kind === "missing") {
    return `Expected ${record.expected_location_code ?? "—"}`;
  }

  return `Found ${record.observed_location_code ?? "—"}`;
}

function investigationEventTitle(
  event: InventoryInvestigation["events"][number],
) {
  if (event.event_type === "opened") return "Reported";
  if (event.event_type === "evidence_added") return "Checked";
  return event.resolution ? statusLabel(event.resolution) : "Resolved";
}

function InvestigationTimeline({
  record,
}: {
  record: InventoryInvestigation;
}) {
  return (
    <div className="investigation-timeline" aria-label="Case history">
      {record.events.map((event) => {
        const detail =
          event.note ||
          event.reason ||
          event.reference ||
          event.location_path ||
          "Case opened";

        return (
          <div className="investigation-event" key={event.id}>
            <span className="investigation-event-index" aria-hidden="true">
              {String(event.sequence).padStart(2, "0")}
            </span>
            <div className="investigation-event-copy">
              <strong>{investigationEventTitle(event)}</strong>
              <span>
                {detail}
                {event.location_code ? ` · ${event.location_code}` : ""}
                {` · ${event.employee_name}`}
              </span>
            </div>
            <time dateTime={event.created_at}>{formatTime(event.created_at)}</time>
          </div>
        );
      })}
    </div>
  );
}

function InvestigationForm({
  token,
  items,
  locations,
  onClose,
  onSaved,
}: WorkflowFormProps) {
  const [kind, setKind] = useState<InvestigationKind>("missing");
  const [barcode, setBarcode] = useState("");
  const [quantity, setQuantity] = useState("1");
  const [expectedLocation, setExpectedLocation] = useState("");
  const [observedLocation, setObservedLocation] = useState("");
  const [reference, setReference] = useState("");
  const [note, setNote] = useState("");
  const [badge, setBadge] = useState("");
  const [pin, setPin] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const attempt = useRef<RetryAttempt | null>(null);

  const item = useMemo(() => findItem(items, barcode), [items, barcode]);
  const needsExpected = kind === "missing" || kind === "misplaced";
  const needsObserved = kind === "misplaced" || kind === "unexpected";

  function changeKind(next: InvestigationKind) {
    setKind(next);
    if (next === "missing") setObservedLocation("");
    if (next === "unexpected") setExpectedLocation("");
    setError(null);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;

    const parsed = Number(quantity);
    if (!item) {
      setError("Scan a known barcode or enter a valid SKU.");
      return;
    }
    if (!Number.isInteger(parsed) || parsed <= 0 || parsed > 1_000_000) {
      setError("Enter a valid quantity.");
      return;
    }
    if (needsExpected && !expectedLocation) {
      setError("Choose the expected location.");
      return;
    }
    if (needsObserved && !observedLocation) {
      setError("Choose the observed location.");
      return;
    }
    if (
      expectedLocation &&
      observedLocation &&
      expectedLocation === observedLocation
    ) {
      setError("Expected and observed locations must be different.");
      return;
    }
    if (!validBadge(badge)) {
      setError("Scan the employee badge.");
      return;
    }
    if (!validPin(pin)) {
      setError("Enter a valid numeric PIN.");
      return;
    }

    const input = {
      kind,
      barcode: item.barcode,
      quantity: parsed,
      ...(needsExpected
        ? { expected_location_code: expectedLocation }
        : {}),
      ...(needsObserved
        ? { observed_location_code: observedLocation }
        : {}),
      ...(reference.trim() ? { reference: reference.trim() } : {}),
      employee_badge_code: badge.trim(),
      ...(pin ? { employee_pin: pin } : {}),
      ...(note.trim() ? { note: note.trim() } : {}),
    };
    const key = attemptKey(
      attempt,
      JSON.stringify(input),
      "investigation",
    );

    setSaving(true);
    setError(null);
    try {
      const response = await stockpileApi.createInvestigation(
        token,
        key,
        input,
      );
      attempt.current = null;
      onSaved(
        `Case ${recordCode(response.data.id)} ${
          response.replayed ? "was already open" : "opened"
        }.`,
      );
    } catch (submissionError) {
      setError(errorText(submissionError, "Case could not be opened."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DialogShell
      id="investigation-dialog-title"
      eyebrow="Stock mismatch"
      title="Open case"
      busy={saving}
      onClose={onClose}
      wide
    >
      <form className="stock-control-form" onSubmit={submit} noValidate>
        <div className="stock-form-grid stock-form-grid-three">
          <div>
            <label htmlFor="investigation-kind">Type</label>
            <select
              id="investigation-kind"
              value={kind}
              onChange={(event) =>
                changeKind(event.target.value as InvestigationKind)
              }
              disabled={saving}
            >
              {INVESTIGATION_KINDS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="investigation-barcode">Barcode or SKU</label>
            <input
              id="investigation-barcode"
              data-autofocus
              value={barcode}
              onChange={(event) => {
                setBarcode(event.target.value);
                setError(null);
              }}
              onKeyDown={(event) => {
                if (event.key !== "Enter") return;
                event.preventDefault();
                const matched = findItem(items, event.currentTarget.value);
                if (!matched) {
                  setError("Scan a known barcode or enter a valid SKU.");
                  return;
                }
                document
                  .getElementById(
                    needsExpected
                      ? "investigation-expected-location"
                      : "investigation-observed-location",
                  )
                  ?.focus();
              }}
              autoComplete="off"
              disabled={saving}
            />
          </div>
          <div>
            <label htmlFor="investigation-quantity">Qty</label>
            <input
              id="investigation-quantity"
              type="number"
              min="1"
              max="1000000"
              step="1"
              inputMode="numeric"
              value={quantity}
              onChange={(event) => {
                setQuantity(event.target.value);
                setError(null);
              }}
              disabled={saving}
            />
          </div>
        </div>

        {item && (
          <div className="stock-resolved-item" aria-live="polite">
            <span className="product-mark" aria-hidden="true">
              {productMark(item)}
            </span>
            <div>
              <strong>{item.product_name}</strong>
              <span>{item.sku}</span>
            </div>
          </div>
        )}

        <div className="stock-form-grid">
          {needsExpected && (
            <div>
              <label htmlFor="investigation-expected-location">Expected</label>
              <select
                id="investigation-expected-location"
                value={expectedLocation}
                onChange={(event) => {
                  setExpectedLocation(event.target.value);
                  setError(null);
                }}
                disabled={saving}
              >
                <option value="">Select location</option>
                {locations.map((location) => (
                  <option key={location.code} value={location.code}>
                    {locationLabel(location)}
                  </option>
                ))}
              </select>
            </div>
          )}
          {needsObserved && (
            <div>
              <label htmlFor="investigation-observed-location">Observed</label>
              <select
                id="investigation-observed-location"
                value={observedLocation}
                onChange={(event) => {
                  setObservedLocation(event.target.value);
                  setError(null);
                }}
                disabled={saving}
              >
                <option value="">Select location</option>
                {locations.map((location) => (
                  <option key={location.code} value={location.code}>
                    {locationLabel(location)}
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>

        <div className="stock-form-grid">
          <div>
            <label htmlFor="investigation-reference">Reference</label>
            <input
              id="investigation-reference"
              value={reference}
              onChange={(event) => setReference(event.target.value)}
              maxLength={120}
              disabled={saving}
            />
          </div>
          <div>
            <label htmlFor="investigation-note">Note</label>
            <input
              id="investigation-note"
              value={note}
              onChange={(event) => setNote(event.target.value)}
              maxLength={500}
              disabled={saving}
            />
          </div>
        </div>

        <BadgeFields
          prefix="investigation-employee"
          label="Employee badge"
          badge={badge}
          pin={pin}
          onBadge={(value) => {
            setBadge(value);
            setError(null);
          }}
          onPin={(value) => {
            setPin(value);
            setError(null);
          }}
          disabled={saving}
        />

        {error && (
          <div className="alert alert-error" role="alert">
            {error}
          </div>
        )}

        <footer>
          <span className="stock-form-total">
            {statusLabel(kind)} · {Number(quantity || 0).toLocaleString("en-PH")} units
          </span>
          <div className="button-row">
            <button
              type="button"
              className="button button-secondary"
              onClick={onClose}
              disabled={saving}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="button button-primary"
              disabled={saving}
            >
              {saving ? "Opening…" : "Open case"}
            </button>
          </div>
        </footer>
      </form>
    </DialogShell>
  );
}

interface InvestigationActionFormProps {
  token: string;
  record: InventoryInvestigation;
  locations: StockLocation[];
  onClose: () => void;
  onSaved: (message: string) => void;
}

function InvestigationReviewForm({
  token,
  record,
  locations,
  onClose,
  onSaved,
}: InvestigationActionFormProps) {
  const [locationCode, setLocationCode] = useState("");
  const [note, setNote] = useState("");
  const [badge, setBadge] = useState("");
  const [pin, setPin] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const attempt = useRef<RetryAttempt | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving || record.status !== "open") return;
    if (!validReason(note)) {
      setError("Enter what was checked.");
      return;
    }
    if (!validBadge(badge)) {
      setError("Scan the employee badge.");
      return;
    }
    if (!validPin(pin)) {
      setError("Enter a valid numeric PIN.");
      return;
    }

    const input = {
      ...(locationCode ? { location_code: locationCode } : {}),
      note: note.trim(),
      employee_badge_code: badge.trim(),
      ...(pin ? { employee_pin: pin } : {}),
    };
    const key = attemptKey(
      attempt,
      JSON.stringify({ investigation: record.id, ...input }),
      "investigation-evidence",
    );

    setSaving(true);
    setError(null);
    try {
      const response = await stockpileApi.addInvestigationEvidence(
        token,
        record.id,
        key,
        input,
      );
      attempt.current = null;
      onSaved(
        `Check ${response.replayed ? "was already saved" : "saved"} to case ${recordCode(
          response.data.id,
        )}.`,
      );
    } catch (submissionError) {
      setError(errorText(submissionError, "Check could not be saved."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DialogShell
      id="investigation-review-title"
      eyebrow={`${statusLabel(record.kind)} ${recordCode(record.id)}`}
      title="Case history"
      busy={saving}
      onClose={onClose}
      wide
    >
      <form className="stock-control-form" onSubmit={submit} noValidate>
        <div className="stock-record-strip">
          <strong>{record.product_name}</strong>
          <span>
            {record.sku} · {record.quantity.toLocaleString("en-PH")} flagged ·{" "}
            {record.inventory_balance_at_open.toLocaleString("en-PH")} recorded ·{" "}
            {investigationLocationSummary(record)}
          </span>
        </div>

        <InvestigationTimeline record={record} />

        {record.status === "open" && (
          <>
            <div className="stock-form-grid">
              <div>
                <label htmlFor="investigation-check-location">Checked location</label>
                <select
                  id="investigation-check-location"
                  value={locationCode}
                  onChange={(event) => {
                    setLocationCode(event.target.value);
                    setError(null);
                  }}
                  disabled={saving}
                >
                  <option value="">No location</option>
                  {locations.map((location) => (
                    <option key={location.code} value={location.code}>
                      {locationLabel(location)}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="investigation-check-note">Check</label>
                <input
                  id="investigation-check-note"
                  data-autofocus
                  value={note}
                  onChange={(event) => {
                    setNote(event.target.value);
                    setError(null);
                  }}
                  minLength={3}
                  maxLength={500}
                  disabled={saving}
                />
              </div>
            </div>

            <BadgeFields
              prefix="investigation-check-employee"
              label="Employee badge"
              badge={badge}
              pin={pin}
              onBadge={(value) => {
                setBadge(value);
                setError(null);
              }}
              onPin={(value) => {
                setPin(value);
                setError(null);
              }}
              disabled={saving}
            />
          </>
        )}

        {error && (
          <div className="alert alert-error" role="alert">
            {error}
          </div>
        )}

        <footer>
          <span className={`status status-${record.status}`}>
            {statusLabel(record.status)}
          </span>
          <div className="button-row">
            <button
              type="button"
              className="button button-secondary"
              onClick={onClose}
              disabled={saving}
            >
              Close
            </button>
            {record.status === "open" && (
              <button
                type="submit"
                className="button button-primary"
                disabled={saving}
              >
                {saving ? "Saving…" : "Add check"}
              </button>
            )}
          </div>
        </footer>
      </form>
    </DialogShell>
  );
}

function InvestigationResolutionForm({
  token,
  record,
  locations,
  onClose,
  onSaved,
}: InvestigationActionFormProps) {
  const [resolution, setResolution] =
    useState<InvestigationResolution>("located");
  const [resolvedLocation, setResolvedLocation] = useState(
    record.observed_location_code ?? record.expected_location_code ?? "",
  );
  const [reference, setReference] = useState("");
  const [reason, setReason] = useState("");
  const [badge, setBadge] = useState("");
  const [pin, setPin] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const attempt = useRef<RetryAttempt | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    if (resolution === "located" && !resolvedLocation) {
      setError("Choose the confirmed location.");
      return;
    }
    if (!validReason(reason)) {
      setError("Enter the resolution reason.");
      return;
    }
    if (!validBadge(badge)) {
      setError("Scan the employee badge.");
      return;
    }
    if (!validPin(pin)) {
      setError("Enter a valid numeric PIN.");
      return;
    }

    const input = {
      resolution,
      ...(resolution === "located"
        ? { resolved_location_code: resolvedLocation }
        : {}),
      ...(reference.trim() ? { reference: reference.trim() } : {}),
      employee_badge_code: badge.trim(),
      ...(pin ? { employee_pin: pin } : {}),
      reason: reason.trim(),
    };
    const key = attemptKey(
      attempt,
      JSON.stringify({ investigation: record.id, ...input }),
      "investigation-resolution",
    );

    setSaving(true);
    setError(null);
    try {
      const response = await stockpileApi.resolveInvestigation(
        token,
        record.id,
        key,
        input,
      );
      attempt.current = null;
      onSaved(
        `Case ${recordCode(response.data.id)} ${
          response.replayed ? "was already closed" : "closed"
        }.`,
      );
    } catch (submissionError) {
      setError(errorText(submissionError, "Case could not be closed."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DialogShell
      id="investigation-resolution-title"
      eyebrow={`${statusLabel(record.kind)} ${recordCode(record.id)}`}
      title="Close case"
      busy={saving}
      onClose={onClose}
    >
      <form className="stock-control-form" onSubmit={submit} noValidate>
        <div className="stock-record-strip">
          <strong>{record.product_name}</strong>
          <span>
            {record.quantity.toLocaleString("en-PH")} units ·{" "}
            {investigationLocationSummary(record)}
          </span>
        </div>

        <div className="stock-form-grid">
          <div>
            <label htmlFor="investigation-resolution">Resolution</label>
            <select
              id="investigation-resolution"
              data-autofocus
              value={resolution}
              onChange={(event) => {
                setResolution(event.target.value as InvestigationResolution);
                setError(null);
              }}
              disabled={saving}
            >
              {INVESTIGATION_RESOLUTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
          {resolution === "located" && (
            <div>
              <label htmlFor="investigation-resolved-location">Location</label>
              <select
                id="investigation-resolved-location"
                value={resolvedLocation}
                onChange={(event) => {
                  setResolvedLocation(event.target.value);
                  setError(null);
                }}
                disabled={saving}
              >
                <option value="">Select location</option>
                {locations.map((location) => (
                  <option key={location.code} value={location.code}>
                    {locationLabel(location)}
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>

        <div className="stock-form-grid">
          <div>
            <label htmlFor="investigation-resolution-reference">Reference</label>
            <input
              id="investigation-resolution-reference"
              value={reference}
              onChange={(event) => setReference(event.target.value)}
              maxLength={120}
              disabled={saving}
            />
          </div>
          <div>
            <label htmlFor="investigation-resolution-reason">Reason</label>
            <input
              id="investigation-resolution-reason"
              value={reason}
              onChange={(event) => {
                setReason(event.target.value);
                setError(null);
              }}
              minLength={3}
              maxLength={500}
              disabled={saving}
            />
          </div>
        </div>

        <BadgeFields
          prefix="investigation-resolution-employee"
          label="Employee badge"
          badge={badge}
          pin={pin}
          onBadge={(value) => {
            setBadge(value);
            setError(null);
          }}
          onPin={(value) => {
            setPin(value);
            setError(null);
          }}
          disabled={saving}
        />

        {error && (
          <div className="alert alert-error" role="alert">
            {error}
          </div>
        )}

        <footer>
          <span className="stock-form-total">Inventory unchanged</span>
          <div className="button-row">
            <button
              type="button"
              className="button button-secondary"
              onClick={onClose}
              disabled={saving}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="button button-primary"
              disabled={saving}
            >
              {saving ? "Closing…" : "Close case"}
            </button>
          </div>
        </footer>
      </form>
    </DialogShell>
  );
}

function ReservationForm({
  token,
  items,
  locations,
  onClose,
  onSaved,
}: WorkflowFormProps) {
  const [lines, setLines] = useState<DraftLine[]>([]);
  const [reference, setReference] = useState("");
  const [customer, setCustomer] = useState("");
  const [channel, setChannel] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const attempt = useRef<RetryAttempt | null>(null);

  const limitFor = useCallback(
    (item: InventoryItem, locationCode: string) =>
      availableAt(item, locationCode),
    [],
  );

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    if (!reference.trim()) {
      setError("Enter the order reference.");
      return;
    }
    if (!validLines(lines, limitFor)) {
      setError("Review the reservation lines.");
      return;
    }
    const input = {
      reference: reference.trim(),
      ...(customer.trim() ? { customer_name: customer.trim() } : {}),
      ...(channel.trim() ? { sales_channel: channel.trim() } : {}),
      ...(note.trim() ? { note: note.trim() } : {}),
      lines: canonicalLines(lines).map((line) => ({
        barcode: line.item.barcode,
        location_code: line.locationCode,
        quantity: line.quantity,
      })),
    };
    const key = attemptKey(attempt, JSON.stringify(input), "reservation");
    setSaving(true);
    setError(null);
    try {
      const response = await stockpileApi.createReservation(token, key, input);
      attempt.current = null;
      onSaved(
        `Reservation ${recordCode(response.data.id)} ${
          response.replayed ? "was already created" : "created"
        }.`,
      );
    } catch (submissionError) {
      setError(
        errorText(submissionError, "Reservation could not be created."),
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <DialogShell
      id="reservation-dialog-title"
      eyebrow="Reservation"
      title="Reserve stock"
      busy={saving}
      onClose={onClose}
      wide
    >
      <form className="stock-control-form" onSubmit={submit} noValidate>
        <div className="stock-form-grid stock-form-grid-three">
          <div>
            <label htmlFor="reservation-reference">Order reference</label>
            <input
              id="reservation-reference"
              value={reference}
              onChange={(event) => {
                setReference(event.target.value);
                setError(null);
              }}
              maxLength={120}
              disabled={saving}
            />
          </div>
          <div>
            <label htmlFor="reservation-customer">Customer</label>
            <input
              id="reservation-customer"
              value={customer}
              onChange={(event) => setCustomer(event.target.value)}
              maxLength={160}
              disabled={saving}
            />
          </div>
          <div>
            <label htmlFor="reservation-channel">Channel</label>
            <input
              id="reservation-channel"
              value={channel}
              onChange={(event) => setChannel(event.target.value)}
              maxLength={80}
              disabled={saving}
            />
          </div>
        </div>

        <LineBuilder
          items={items}
          locations={locations}
          lines={lines}
          onChange={(next) => {
            setLines(next);
            setError(null);
          }}
          disabled={saving}
          limitFor={limitFor}
        />

        <div>
          <label htmlFor="reservation-note">Note</label>
          <input
            id="reservation-note"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            maxLength={500}
            disabled={saving}
          />
        </div>

        {error && (
          <div className="alert alert-error" role="alert">
            {error}
          </div>
        )}

        <footer>
          <span className="stock-form-total">
            {totalLines(lines).toLocaleString("en-PH")} units
          </span>
          <div className="button-row">
            <button
              type="button"
              className="button button-secondary"
              onClick={onClose}
              disabled={saving}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="button button-primary"
              disabled={saving || !lines.length}
            >
              {saving ? "Reserving…" : "Reserve"}
            </button>
          </div>
        </footer>
      </form>
    </DialogShell>
  );
}

interface ReservationCloseFormProps {
  token: string;
  record: InventoryReservation;
  action: ReservationAction;
  onClose: () => void;
  onSaved: (message: string) => void;
}

function ReservationCloseForm({
  token,
  record,
  action,
  onClose,
  onSaved,
}: ReservationCloseFormProps) {
  const [reason, setReason] = useState("");
  const [badge, setBadge] = useState("");
  const [pin, setPin] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const attempt = useRef<RetryAttempt | null>(null);
  const fulfilling = action === "fulfill";

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    if (fulfilling && !validBadge(badge)) {
      setError("Scan the employee badge.");
      return;
    }
    if (!validPin(pin)) {
      setError("Enter a valid numeric PIN.");
      return;
    }
    const input = {
      action,
      ...(reason.trim() ? { reason: reason.trim() } : {}),
      ...(fulfilling && badge.trim()
        ? { employee_badge_code: badge.trim() }
        : {}),
      ...(fulfilling && pin ? { employee_pin: pin } : {}),
    };
    const signature = JSON.stringify({ reservation: record.id, ...input });
    const key = attemptKey(attempt, signature, `reservation-${action}`);
    setSaving(true);
    setError(null);
    try {
      const response = await stockpileApi.closeReservation(
        token,
        record.id,
        key,
        input,
      );
      attempt.current = null;
      const state = fulfilling ? "fulfilled" : "released";
      onSaved(
        `Reservation ${recordCode(response.data.id)} ${
          response.replayed ? `was already ${state}` : state
        }.`,
      );
    } catch (submissionError) {
      setError(errorText(submissionError, "Reservation could not be closed."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DialogShell
      id="reservation-close-title"
      eyebrow={`Reservation ${recordCode(record.id)}`}
      title={fulfilling ? "Fulfill order" : "Release stock"}
      busy={saving}
      onClose={onClose}
    >
      <form className="stock-control-form" onSubmit={submit} noValidate>
        <div className="stock-record-strip">
          <strong>{record.reference}</strong>
          <span>
            {record.total_quantity.toLocaleString("en-PH")} units
            {record.customer_name ? ` · ${record.customer_name}` : ""}
          </span>
        </div>

        <div>
          <label htmlFor="reservation-close-reason">Reason</label>
          <input
            id="reservation-close-reason"
            data-autofocus
            value={reason}
            onChange={(event) => {
              setReason(event.target.value);
              setError(null);
            }}
            maxLength={500}
            disabled={saving}
          />
        </div>

        {fulfilling && (
          <BadgeFields
            prefix="reservation-fulfillment"
            label="Employee badge"
            badge={badge}
            pin={pin}
            onBadge={(value) => {
              setBadge(value);
              setError(null);
            }}
            onPin={(value) => {
              setPin(value);
              setError(null);
            }}
            disabled={saving}
          />
        )}

        {error && (
          <div className="alert alert-error" role="alert">
            {error}
          </div>
        )}

        <footer>
          <span />
          <div className="button-row">
            <button
              type="button"
              className="button button-secondary"
              onClick={onClose}
              disabled={saving}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="button button-primary"
              disabled={saving}
            >
              {saving
                ? fulfilling
                  ? "Fulfilling…"
                  : "Releasing…"
                : fulfilling
                  ? "Fulfill"
                  : "Release"}
            </button>
          </div>
        </footer>
      </form>
    </DialogShell>
  );
}

function CountForm({
  token,
  items,
  locations,
  onClose,
  onSaved,
}: WorkflowFormProps) {
  const [locationCode, setLocationCode] = useState("");
  const [barcode, setBarcode] = useState("");
  const [lines, setLines] = useState<CountDraftLine[]>([]);
  const [badge, setBadge] = useState("");
  const [pin, setPin] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const attempt = useRef<RetryAttempt | null>(null);
  const scanRef = useRef<HTMLInputElement>(null);
  const countInputRefs = useRef<Record<string, HTMLInputElement | null>>({});

  const remaining = lines.filter((line) => line.quantity === "").length;

  function chooseLocation(code: string) {
    setLocationCode(code);
    setBarcode("");
    setError(null);
    setLines(
      items
        .filter(
          (item) =>
            quantityAt(item, code) > 0,
        )
        .sort((left, right) =>
          left.product_name.localeCompare(right.product_name),
        )
        .map((item) => ({
          item,
          expected: quantityAt(item, code),
          quantity: "",
        })),
    );
    requestAnimationFrame(() => scanRef.current?.focus());
  }

  function scanItem() {
    setError(null);
    if (!locationCode) {
      setError("Choose a location.");
      return;
    }
    const item = findItem(items, barcode);
    if (!item) {
      setError("Product not found.");
      return;
    }
    const existing = lines.find((line) => line.item.sku === item.sku);
    if (!existing) {
      setLines((current) => [
        ...current,
        {
          item,
          expected: quantityAt(item, locationCode),
          quantity: "",
        },
      ]);
    }
    setBarcode("");
    requestAnimationFrame(() =>
      countInputRefs.current[item.sku]?.focus(),
    );
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    if (!locationCode) {
      setError("Choose a location.");
      return;
    }
    if (!lines.length) {
      setError("No products were counted.");
      return;
    }
    if (lines.length > 5_000) {
      setError("This count has too many products.");
      return;
    }
    const parsed = lines.map((line) => ({
      ...line,
      counted: Number(line.quantity),
    }));
    if (
      parsed.some(
        (line) =>
          line.quantity === "" ||
          !Number.isInteger(line.counted) ||
          line.counted < 0 ||
          line.counted > 1_000_000,
      )
    ) {
      setError("Enter every counted quantity.");
      return;
    }
    if (!validBadge(badge)) {
      setError("Scan the counter badge.");
      return;
    }
    if (!validPin(pin)) {
      setError("Enter a valid numeric PIN.");
      return;
    }
    const input = {
      location_code: locationCode,
      counter_badge_code: badge.trim(),
      ...(pin ? { counter_pin: pin } : {}),
      ...(note.trim() ? { note: note.trim() } : {}),
      lines: parsed
        .map((line) => ({
          barcode: line.item.barcode,
          quantity: line.counted,
        }))
        .sort((left, right) => left.barcode.localeCompare(right.barcode)),
    };
    const key = attemptKey(attempt, JSON.stringify(input), "stock-count");
    setSaving(true);
    setError(null);
    try {
      const response = await stockpileApi.createStockCount(token, key, input);
      attempt.current = null;
      onSaved(
        `Count ${recordCode(response.data.id)} ${
          response.replayed ? "was already saved" : "pending"
        }.`,
      );
    } catch (submissionError) {
      setError(errorText(submissionError, "Count could not be saved."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DialogShell
      id="count-dialog-title"
      eyebrow="Physical count"
      title="Count location"
      busy={saving}
      onClose={onClose}
      wide
    >
      <form className="stock-control-form" onSubmit={submit} noValidate>
        <div>
          <label htmlFor="count-location">Location</label>
          <select
            id="count-location"
            data-autofocus
            value={locationCode}
            onChange={(event) => chooseLocation(event.target.value)}
            disabled={saving || lines.some((line) => line.quantity !== "")}
          >
            <option value="">Select location</option>
            {locations.map((location) => (
              <option key={location.code} value={location.code}>
                {locationLabel(location)}
              </option>
            ))}
          </select>
        </div>

        <div className="count-scan-row">
          <div>
            <label htmlFor="count-barcode">Barcode or SKU</label>
            <input
              ref={scanRef}
              id="count-barcode"
              value={barcode}
              onChange={(event) => {
                setBarcode(event.target.value);
                setError(null);
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  scanItem();
                }
              }}
              autoComplete="off"
              disabled={saving || !locationCode}
            />
          </div>
          <button
            type="button"
            className="button button-secondary"
            onClick={scanItem}
            disabled={saving || !locationCode}
          >
            Find
          </button>
        </div>

        <div className="count-draft" aria-label="Counted products">
          {lines.length ? (
            lines.map((line) => (
              <div className="count-draft-line" key={line.item.sku}>
                <span className="product-mark" aria-hidden="true">
                  {productMark(line.item)}
                </span>
                <div>
                  <strong>{line.item.product_name}</strong>
                  <span>
                    {line.item.sku}
                  </span>
                </div>
                <div>
                  <label htmlFor={`counted-${line.item.sku}`}>Counted</label>
                  <input
                    ref={(element) => {
                      countInputRefs.current[line.item.sku] = element;
                    }}
                    id={`counted-${line.item.sku}`}
                    type="number"
                    min="0"
                    max="1000000"
                    step="1"
                    inputMode="numeric"
                    value={line.quantity}
                    onChange={(event) => {
                      const value = event.target.value;
                      setLines((current) =>
                        current.map((entry) =>
                          entry.item.sku === line.item.sku
                            ? { ...entry, quantity: value }
                            : entry,
                        ),
                      );
                      setError(null);
                    }}
                    disabled={saving}
                  />
                </div>
              </div>
            ))
          ) : (
            <div className="stock-draft-empty">
              {locationCode ? "No recorded stock." : "Choose a location."}
            </div>
          )}
        </div>

        <BadgeFields
          prefix="count-counter"
          label="Counter badge"
          badge={badge}
          pin={pin}
          onBadge={(value) => {
            setBadge(value);
            setError(null);
          }}
          onPin={(value) => {
            setPin(value);
            setError(null);
          }}
          disabled={saving}
        />

        <div>
          <label htmlFor="count-note">Note</label>
          <input
            id="count-note"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            maxLength={500}
            disabled={saving}
          />
        </div>

        {error && (
          <div className="alert alert-error" role="alert">
            {error}
          </div>
        )}

        <footer>
          <span className="stock-form-total">
            {remaining.toLocaleString("en-PH")} remaining
          </span>
          <div className="button-row">
            <button
              type="button"
              className="button button-secondary"
              onClick={onClose}
              disabled={saving}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="button button-primary"
              disabled={saving || !lines.length || remaining > 0}
            >
              {saving ? "Saving…" : "Save count"}
            </button>
          </div>
        </footer>
      </form>
    </DialogShell>
  );
}

interface CountCloseFormProps {
  token: string;
  record: InventoryStockCount;
  action: "approve" | "cancel";
  onClose: () => void;
  onSaved: (message: string) => void;
}

function CountCloseForm({
  token,
  record,
  action,
  onClose,
  onSaved,
}: CountCloseFormProps) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const attempt = useRef<RetryAttempt | null>(null);
  const approving = action === "approve";

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    if (!validReason(reason)) {
      setError("Enter the reason.");
      return;
    }
    const input = { reason: reason.trim() };
    const signature = JSON.stringify({ count: record.id, action, ...input });
    const key = attemptKey(attempt, signature, `count-${action}`);
    setSaving(true);
    setError(null);
    try {
      const response = approving
        ? await stockpileApi.approveStockCount(token, record.id, key, input)
        : await stockpileApi.cancelStockCount(token, record.id, key, input);
      attempt.current = null;
      const state = approving ? "applied" : "cancelled";
      onSaved(
        `Count ${recordCode(response.data.id)} ${
          response.replayed ? `was already ${state}` : state
        }.`,
      );
    } catch (submissionError) {
      setError(errorText(submissionError, "Count could not be closed."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DialogShell
      id="count-close-title"
      eyebrow={`Count ${recordCode(record.id)}`}
      title={approving ? "Apply count" : "Cancel count"}
      busy={saving}
      onClose={onClose}
    >
      <form className="stock-control-form" onSubmit={submit} noValidate>
        <div className="stock-record-strip">
          <strong>{record.location_code}</strong>
          <span>
            {record.discrepancy_lines.toLocaleString("en-PH")} differences ·{" "}
            {record.difference > 0 ? "+" : ""}
            {record.difference.toLocaleString("en-PH")} units
          </span>
        </div>

        {record.lines.some((line) => line.difference !== 0) && (
          <div className="count-review-lines" aria-label="Count differences">
            <div className="count-review-head" aria-hidden="true">
              <span>Item</span>
              <span>Expected</span>
              <span>Counted</span>
              <span>Difference</span>
            </div>
            {record.lines
              .filter((line) => line.difference !== 0)
              .map((line) => (
                <div className="count-review-line" key={line.sku}>
                  <div>
                    <strong>{line.product_name}</strong>
                    <span>{line.sku}</span>
                  </div>
                  <span
                    aria-label={`Expected ${line.expected_quantity.toLocaleString("en-PH")}`}
                  >
                    {line.expected_quantity.toLocaleString("en-PH")}
                  </span>
                  <span
                    aria-label={`Counted ${line.counted_quantity.toLocaleString("en-PH")}`}
                  >
                    {line.counted_quantity.toLocaleString("en-PH")}
                  </span>
                  <strong
                    aria-label={`Difference ${line.difference > 0 ? "plus " : ""}${line.difference.toLocaleString("en-PH")}`}
                  >
                    {line.difference > 0 ? "+" : ""}
                    {line.difference.toLocaleString("en-PH")}
                  </strong>
                </div>
              ))}
          </div>
        )}

        <div>
          <label htmlFor="count-close-reason">Reason</label>
          <input
            id="count-close-reason"
            data-autofocus
            value={reason}
            onChange={(event) => {
              setReason(event.target.value);
              setError(null);
            }}
            maxLength={500}
            disabled={saving}
          />
        </div>

        {error && (
          <div className="alert alert-error" role="alert">
            {error}
          </div>
        )}

        <footer>
          <span />
          <div className="button-row">
            <button
              type="button"
              className="button button-secondary"
              onClick={onClose}
              disabled={saving}
            >
              Back
            </button>
            <button
              type="submit"
              className={
                "button " +
                (approving ? "button-primary" : "button-danger")
              }
              disabled={saving}
            >
              {saving
                ? approving
                  ? "Applying…"
                  : "Cancelling…"
                : approving
                  ? "Apply"
                  : "Cancel count"}
            </button>
          </div>
        </footer>
      </form>
    </DialogShell>
  );
}

const VIEW_LABELS: Record<StockControlView, string> = {
  receive: "Receive",
  move: "Move",
  orders: "Orders",
  issues: "Issues",
  count: "Count",
};

function statusLabel(value: string) {
  return value
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

function RegisterEmpty({ label }: { label: string }) {
  return (
    <div className="empty-state compact">
      <strong>{label}</strong>
    </div>
  );
}

export default function StockControlPanel({
  token,
  role,
  items,
  locations,
  onChanged,
  embedded = false,
}: StockControlPanelProps) {
  const [activeView, setActiveView] =
    useState<StockControlView>("receive");
  const [selectedIssueView, setSelectedIssueView] =
    useState<IssueView | null>(null);
  const [deliveries, setDeliveries] = useState<InventoryDelivery[]>([]);
  const [returns, setReturns] = useState<InventoryReturn[]>([]);
  const [damageCases, setDamageCases] = useState<DamageCase[]>([]);
  const [investigations, setInvestigations] =
    useState<InventoryInvestigation[]>([]);
  const [reservations, setReservations] =
    useState<InventoryReservation[]>([]);
  const [counts, setCounts] = useState<InventoryStockCount[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [dialog, setDialog] = useState<DialogState>(null);

  const activeLocations = useMemo(
    () =>
      locations
        .filter(
          (location) =>
            location.active &&
            location.code !== UNASSIGNED,
        )
        .sort((left, right) =>
          locationLabel(left).localeCompare(locationLabel(right)),
        ),
    [locations],
  );

  const stockableLocations = useMemo(
    () => activeLocations.filter((location) => location.stockable),
    [activeLocations],
  );

  const loadAll = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [
        nextDeliveries,
        nextReturns,
        nextDamage,
        nextInvestigations,
        nextReservations,
        nextCounts,
      ] = await Promise.all([
        stockpileApi.deliveries(token),
        stockpileApi.returns(token),
        stockpileApi.damageCases(token),
        stockpileApi.investigations(token),
        stockpileApi.reservations(token),
        stockpileApi.stockCounts(token),
      ]);
      setDeliveries(nextDeliveries);
      setReturns(nextReturns);
      setDamageCases(nextDamage);
      setInvestigations(nextInvestigations);
      setReservations(nextReservations);
      setCounts(nextCounts);
    } catch (error) {
      setLoadError(errorText(error, "Stock records could not be loaded."));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  const openDamageCount = useMemo(
    () => damageCases.filter((record) => record.status === "open").length,
    [damageCases],
  );

  const openInvestigationCount = useMemo(
    () => investigations.filter((record) => record.status === "open").length,
    [investigations],
  );

  const issueView: IssueView =
    selectedIssueView ??
    (openInvestigationCount > 0 || openDamageCount === 0
      ? "investigations"
      : "damage");

  const viewCounts = useMemo<Record<StockControlView, number | null>>(
    () => ({
      receive: deliveries.length,
      move: null,
      orders: reservations.filter(
        (record) => record.status === "active",
      ).length,
      issues: openInvestigationCount + openDamageCount,
      count: counts.filter((record) => record.status === "pending").length,
    }),
    [
      counts,
      deliveries,
      openDamageCount,
      openInvestigationCount,
      reservations,
    ],
  );

  const activeRecordView: StockRecordView =
    activeView === "receive"
      ? "deliveries"
      : activeView === "orders"
        ? "reservations"
        : activeView === "count"
          ? "counts"
          : activeView === "issues"
            ? issueView
            : "deliveries";

  const actionLabel =
    activeView === "receive"
      ? "Receive delivery"
      : activeView === "orders"
        ? "Reserve stock"
        : activeView === "count"
          ? "Start count"
          : activeView === "issues"
            ? issueView === "investigations"
              ? "Open case"
              : issueView === "damage"
                ? "Report damage"
                : "Record return"
            : null;

  const orderedInvestigations = useMemo(
    () =>
      [...investigations].sort(
        (left, right) =>
          Number(right.status === "open") - Number(left.status === "open") ||
          new Date(right.created_at).getTime() -
            new Date(left.created_at).getTime(),
      ),
    [investigations],
  );

  const orderedDamageCases = useMemo(
    () =>
      [...damageCases].sort(
        (left, right) =>
          Number(right.status === "open") - Number(left.status === "open"),
      ),
    [damageCases],
  );

  const orderedReservations = useMemo(
    () =>
      [...reservations].sort(
        (left, right) =>
          Number(right.status === "active") -
          Number(left.status === "active"),
      ),
    [reservations],
  );

  const orderedCounts = useMemo(
    () =>
      [...counts].sort(
        (left, right) =>
          Number(right.status === "pending") -
          Number(left.status === "pending"),
      ),
    [counts],
  );

  const finishWorkflow = useCallback(
    (message: string) => {
      setDialog(null);
      setNotice(message);
      void Promise.allSettled([loadAll(), Promise.resolve(onChanged())]);
    },
    [loadAll, onChanged],
  );

  function openCreate() {
    setNotice(null);
    if (activeView === "receive") setDialog({ kind: "delivery" });
    if (activeView === "orders") setDialog({ kind: "reservation" });
    if (activeView === "count") setDialog({ kind: "count" });
    if (activeView === "issues" && issueView === "damage") {
      setDialog({ kind: "damage" });
    }
    if (activeView === "issues" && issueView === "investigations") {
      setDialog({ kind: "investigation" });
    }
    if (activeView === "issues" && issueView === "returns") {
      setDialog({ kind: "return" });
    }
  }

  function moveTab(event: ReactKeyboardEvent<HTMLButtonElement>) {
    if (![
      "ArrowLeft",
      "ArrowRight",
      "Home",
      "End",
    ].includes(event.key)) {
      return;
    }
    event.preventDefault();
    const views = Object.keys(VIEW_LABELS) as StockControlView[];
    const current = views.indexOf(activeView);
    const nextIndex =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? views.length - 1
          : event.key === "ArrowRight"
            ? (current + 1) % views.length
            : (current - 1 + views.length) % views.length;
    const next = views[nextIndex];
    setActiveView(next);
    setNotice(null);
    document.getElementById(`stock-${next}-tab`)?.focus();
  }

  function moveIssueTab(event: ReactKeyboardEvent<HTMLButtonElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      return;
    }
    event.preventDefault();
    const views: IssueView[] = ["investigations", "damage", "returns"];
    const current = views.indexOf(issueView);
    const nextIndex =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? views.length - 1
          : event.key === "ArrowRight"
            ? (current + 1) % views.length
            : (current - 1 + views.length) % views.length;
    const next = views[nextIndex];
    setSelectedIssueView(next);
    setNotice(null);
    document.getElementById(`stock-issues-${next}-tab`)?.focus();
  }

  function selectView(view: StockControlView) {
    setActiveView(view);
    setNotice(null);
  }

  function selectIssueView(view: IssueView) {
    setSelectedIssueView(view);
    setNotice(null);
  }

  const createDisabled =
    !(activeView === "issues" && issueView === "investigations"
      ? activeLocations.length
      : stockableLocations.length) ||
    !items.some((item) => item.active !== false);

  return (
    <section
      className={
        (embedded ? "ledger-panel" : "panel") + " stock-control-ledger"
      }
      aria-label="Stock actions"
    >
      <div className="stock-control-head">
        <div
          className="stock-control-tabs"
          role="tablist"
          aria-label="Stock actions"
        >
          {(Object.keys(VIEW_LABELS) as StockControlView[]).map((view) => {
            const count = viewCounts[view];
            return (
              <button
                id={`stock-${view}-tab`}
                key={view}
                type="button"
                role="tab"
                aria-selected={activeView === view}
                aria-controls="stock-control-panel"
                tabIndex={activeView === view ? 0 : -1}
                className={activeView === view ? "active" : ""}
                onClick={() => selectView(view)}
                onKeyDown={moveTab}
              >
                {VIEW_LABELS[view]}
                {count !== null && (
                  <span>{count.toLocaleString("en-PH")}</span>
                )}
              </button>
            );
          })}
        </div>
        {actionLabel && (
          <button
            type="button"
            className="button button-primary stock-control-action"
            onClick={openCreate}
            disabled={createDisabled}
          >
            {actionLabel}
          </button>
        )}
      </div>

      {notice && (
        <div className="stock-control-notice" role="status">
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

      <div
        id="stock-control-panel"
        className="stock-control-stage"
        role="tabpanel"
        aria-labelledby={`stock-${activeView}-tab`}
      >
        {activeView === "issues" && !loading && !loadError && (
          <div
            className="stock-issue-switch"
            role="tablist"
            aria-label="Issue register"
          >
            <button
              id="stock-issues-investigations-tab"
              type="button"
              role="tab"
              aria-selected={issueView === "investigations"}
              aria-controls="stock-issues-panel"
              tabIndex={issueView === "investigations" ? 0 : -1}
              className={issueView === "investigations" ? "active" : ""}
              onClick={() => selectIssueView("investigations")}
              onKeyDown={moveIssueTab}
            >
              Stock checks{" "}
              <span>{openInvestigationCount.toLocaleString("en-PH")}</span>
            </button>
            <button
              id="stock-issues-damage-tab"
              type="button"
              role="tab"
              aria-selected={issueView === "damage"}
              aria-controls="stock-issues-panel"
              tabIndex={issueView === "damage" ? 0 : -1}
              className={issueView === "damage" ? "active" : ""}
              onClick={() => selectIssueView("damage")}
              onKeyDown={moveIssueTab}
            >
              Damage <span>{openDamageCount.toLocaleString("en-PH")}</span>
            </button>
            <button
              id="stock-issues-returns-tab"
              type="button"
              role="tab"
              aria-selected={issueView === "returns"}
              aria-controls="stock-issues-panel"
              tabIndex={issueView === "returns" ? 0 : -1}
              className={issueView === "returns" ? "active" : ""}
              onClick={() => selectIssueView("returns")}
              onKeyDown={moveIssueTab}
            >
              Returns <span>{returns.length.toLocaleString("en-PH")}</span>
            </button>
          </div>
        )}

        {activeView === "move" ? (
          <TransferPanel
            token={token}
            items={items}
            locations={locations}
            onChanged={onChanged}
            embedded
          />
        ) : loading ? (
          <div className="loading-state panel-state" role="status">
            <span className="spinner" aria-hidden="true" />
            Loading…
          </div>
        ) : loadError ? (
          <div className="recovery-state panel-state" role="alert">
            <div>
              <strong>Records unavailable</strong>
              <span>{loadError}</span>
            </div>
            <button
              type="button"
              className="button button-secondary"
              onClick={() => void loadAll()}
            >
              Retry
            </button>
          </div>
        ) : activeRecordView === "deliveries" ? (
          <div className="stock-control-register">
            {deliveries.length ? (
              deliveries.map((record) => (
                <article className="stock-control-row" key={record.id}>
                  <div className="stock-record-code">
                    <span>Delivery</span>
                    <strong>{recordCode(record.id)}</strong>
                  </div>
                  <div className="stock-record-primary">
                    <strong>{record.supplier_name || "Supplier"}</strong>
                    <span>{record.reference || "No reference"}</span>
                  </div>
                  <div className="stock-record-lines">
                    <strong>
                      {totalLines(record.lines).toLocaleString("en-PH")} units
                    </strong>
                    <span>
                      {record.lines.length.toLocaleString("en-PH")} items
                    </span>
                  </div>
                  <div className="stock-record-person">
                    <strong>{record.receiver_name}</strong>
                    <span>{formatTime(record.received_at)}</span>
                  </div>
                  <span className="status status-received">Received</span>
                </article>
              ))
            ) : (
              <RegisterEmpty label="No deliveries." />
            )}
          </div>
        ) : activeRecordView === "investigations" ? (
          <div
            id="stock-issues-panel"
            className="stock-control-register"
            role="tabpanel"
            aria-labelledby="stock-issues-investigations-tab"
          >
            {investigations.length ? (
              orderedInvestigations.map((record) => (
                <article className="stock-control-row" key={record.id}>
                  <div className="stock-record-code">
                    <span>{statusLabel(record.kind)}</span>
                    <strong>{recordCode(record.id)}</strong>
                  </div>
                  <div className="stock-record-primary">
                    <strong>{record.product_name}</strong>
                    <span>{record.sku}</span>
                  </div>
                  <div className="stock-record-lines">
                    <strong>
                      {record.quantity.toLocaleString("en-PH")} units
                    </strong>
                    <span>{investigationLocationSummary(record)}</span>
                  </div>
                  <div className="stock-record-person">
                    <strong>{record.opened_employee_name}</strong>
                    <span>{formatTime(record.created_at)}</span>
                  </div>
                  <div className="stock-record-actions">
                    <span className={`status status-${record.status}`}>
                      {statusLabel(record.status)}
                    </span>
                    <div className="stock-row-buttons">
                      <button
                        type="button"
                        className="text-button"
                        onClick={() =>
                          setDialog({
                            kind: "investigation-review",
                            record,
                          })
                        }
                      >
                        Review
                      </button>
                      {role === "admin" && record.status === "open" && (
                        <button
                          type="button"
                          className="text-button"
                          onClick={() =>
                            setDialog({
                              kind: "investigation-resolution",
                              record,
                            })
                          }
                        >
                          Resolve
                        </button>
                      )}
                    </div>
                  </div>
                </article>
              ))
            ) : (
              <RegisterEmpty label="No stock checks." />
            )}
          </div>
        ) : activeRecordView === "returns" ? (
          <div
            id="stock-issues-panel"
            className="stock-control-register"
            role="tabpanel"
            aria-labelledby="stock-issues-returns-tab"
          >
            {returns.length ? (
              returns.map((record) => (
                <article className="stock-control-row" key={record.id}>
                  <div className="stock-record-code">
                    <span>Return</span>
                    <strong>{recordCode(record.id)}</strong>
                  </div>
                  <div className="stock-record-primary">
                    <strong>
                      {record.return_type === "customer_return"
                        ? "Customer"
                        : "Supplier"}
                    </strong>
                    <span>{record.party_name || record.reference || "—"}</span>
                  </div>
                  <div className="stock-record-lines">
                    <strong>
                      {totalLines(record.lines).toLocaleString("en-PH")} units
                    </strong>
                    <span>
                      {record.lines.length.toLocaleString("en-PH")} items
                    </span>
                  </div>
                  <div className="stock-record-person">
                    <strong>{record.employee_name}</strong>
                    <span>{formatTime(record.completed_at)}</span>
                  </div>
                  <span className="status status-completed">Completed</span>
                </article>
              ))
            ) : (
              <RegisterEmpty label="No returns." />
            )}
          </div>
        ) : activeRecordView === "damage" ? (
          <div
            id="stock-issues-panel"
            className="stock-control-register"
            role="tabpanel"
            aria-labelledby="stock-issues-damage-tab"
          >
            {damageCases.length ? (
              orderedDamageCases.map((record) => (
                <article className="stock-control-row" key={record.id}>
                  <div className="stock-record-code">
                    <span>Damage</span>
                    <strong>{recordCode(record.id)}</strong>
                  </div>
                  <div className="stock-record-primary">
                    <strong>{record.product_name}</strong>
                    <span>
                      {record.sku} · {record.location_code}
                    </span>
                  </div>
                  <div className="stock-record-lines">
                    <strong>
                      {record.remaining_quantity.toLocaleString("en-PH")} units
                    </strong>
                    <span>{statusLabel(record.category)}</span>
                  </div>
                  <div className="stock-record-person">
                    <strong>{record.reported_by_name}</strong>
                    <span>{formatTime(record.created_at)}</span>
                  </div>
                  <div className="stock-record-actions">
                    <span className={`status status-${record.status}`}>
                      {statusLabel(record.status)}
                    </span>
                    {role === "admin" && record.status === "open" && (
                      <button
                        type="button"
                        className="text-button"
                        onClick={() =>
                          setDialog({
                            kind: "damage-resolution",
                            record,
                          })
                        }
                      >
                        Resolve
                      </button>
                    )}
                  </div>
                </article>
              ))
            ) : (
              <RegisterEmpty label="No damage records." />
            )}
          </div>
        ) : activeRecordView === "reservations" ? (
          <div className="stock-control-register">
            {reservations.length ? (
              orderedReservations.map((record) => (
                <article className="stock-control-row" key={record.id}>
                  <div className="stock-record-code">
                    <span>Order</span>
                    <strong>{record.reference}</strong>
                  </div>
                  <div className="stock-record-primary">
                    <strong>{record.customer_name || "Customer"}</strong>
                    <span>{record.sales_channel || recordCode(record.id)}</span>
                  </div>
                  <div className="stock-record-lines">
                    <strong>
                      {record.total_quantity.toLocaleString("en-PH")} units
                    </strong>
                    <span>
                      {record.lines.length.toLocaleString("en-PH")} items
                    </span>
                  </div>
                  <div className="stock-record-person">
                    <strong>{record.created_by_name}</strong>
                    <span>{formatTime(record.created_at)}</span>
                  </div>
                  <div className="stock-record-actions">
                    <span className={`status status-${record.status}`}>
                      {statusLabel(record.status)}
                    </span>
                    {record.status === "active" && (
                      <div className="stock-row-buttons">
                        <button
                          type="button"
                          className="text-button"
                          onClick={() =>
                            setDialog({
                              kind: "reservation-close",
                              record,
                              action: "release",
                            })
                          }
                        >
                          Release
                        </button>
                        <button
                          type="button"
                          className="text-button"
                          onClick={() =>
                            setDialog({
                              kind: "reservation-close",
                              record,
                              action: "fulfill",
                            })
                          }
                        >
                          Fulfill
                        </button>
                      </div>
                    )}
                  </div>
                </article>
              ))
            ) : (
              <RegisterEmpty label="No reservations." />
            )}
          </div>
        ) : (
          <div className="stock-control-register">
            {counts.length ? (
              orderedCounts.map((record) => (
                <article className="stock-control-row" key={record.id}>
                  <div className="stock-record-code">
                    <span>Count</span>
                    <strong>{recordCode(record.id)}</strong>
                  </div>
                  <div className="stock-record-primary">
                    <strong>{record.location_code}</strong>
                    <span>{record.location_path}</span>
                  </div>
                  <div className="stock-record-lines">
                    <strong>
                      {record.discrepancy_lines.toLocaleString("en-PH")} differences
                    </strong>
                    <span>
                      {record.difference > 0 ? "+" : ""}
                      {record.difference.toLocaleString("en-PH")} units
                    </span>
                  </div>
                  <div className="stock-record-person">
                    <strong>{record.counter_name}</strong>
                    <span>{formatTime(record.created_at)}</span>
                  </div>
                  <div className="stock-record-actions">
                    <span className={`status status-${record.status}`}>
                      {statusLabel(record.status)}
                    </span>
                    {role === "admin" && record.status === "pending" && (
                      <div className="stock-row-buttons">
                        <button
                          type="button"
                          className="text-button danger-text"
                          onClick={() =>
                            setDialog({
                              kind: "count-close",
                              record,
                              action: "cancel",
                            })
                          }
                        >
                          Cancel
                        </button>
                        <button
                          type="button"
                          className="text-button"
                          onClick={() =>
                            setDialog({
                              kind: "count-close",
                              record,
                              action: "approve",
                            })
                          }
                        >
                          Apply
                        </button>
                      </div>
                    )}
                  </div>
                </article>
              ))
            ) : (
              <RegisterEmpty label="No counts." />
            )}
          </div>
        )}
      </div>

      {dialog?.kind === "delivery" && (
        <DeliveryForm
          token={token}
          items={items}
          locations={stockableLocations}
          onClose={() => setDialog(null)}
          onSaved={finishWorkflow}
        />
      )}
      {dialog?.kind === "return" && (
        <ReturnForm
          token={token}
          items={items}
          locations={stockableLocations}
          onClose={() => setDialog(null)}
          onSaved={finishWorkflow}
        />
      )}
      {dialog?.kind === "damage" && (
        <DamageForm
          token={token}
          items={items}
          locations={stockableLocations}
          onClose={() => setDialog(null)}
          onSaved={finishWorkflow}
        />
      )}
      {dialog?.kind === "investigation" && (
        <InvestigationForm
          token={token}
          items={items}
          locations={activeLocations}
          onClose={() => setDialog(null)}
          onSaved={finishWorkflow}
        />
      )}
      {dialog?.kind === "reservation" && (
        <ReservationForm
          token={token}
          items={items}
          locations={stockableLocations}
          onClose={() => setDialog(null)}
          onSaved={finishWorkflow}
        />
      )}
      {dialog?.kind === "count" && (
        <CountForm
          token={token}
          items={items}
          locations={stockableLocations}
          onClose={() => setDialog(null)}
          onSaved={finishWorkflow}
        />
      )}
      {dialog?.kind === "damage-resolution" && (
        <DamageResolutionForm
          token={token}
          record={dialog.record}
          onClose={() => setDialog(null)}
          onSaved={finishWorkflow}
        />
      )}
      {dialog?.kind === "investigation-review" && (
        <InvestigationReviewForm
          token={token}
          record={dialog.record}
          locations={activeLocations}
          onClose={() => setDialog(null)}
          onSaved={finishWorkflow}
        />
      )}
      {dialog?.kind === "investigation-resolution" && (
        <InvestigationResolutionForm
          token={token}
          record={dialog.record}
          locations={activeLocations}
          onClose={() => setDialog(null)}
          onSaved={finishWorkflow}
        />
      )}
      {dialog?.kind === "reservation-close" && (
        <ReservationCloseForm
          token={token}
          record={dialog.record}
          action={dialog.action}
          onClose={() => setDialog(null)}
          onSaved={finishWorkflow}
        />
      )}
      {dialog?.kind === "count-close" && (
        <CountCloseForm
          token={token}
          record={dialog.record}
          action={dialog.action}
          onClose={() => setDialog(null)}
          onSaved={finishWorkflow}
        />
      )}
    </section>
  );
}
