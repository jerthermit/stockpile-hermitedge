"use client";

import { ChangeEvent, FormEvent, useRef, useState } from "react";
import { ApiError, stockpileApi } from "../lib/api";
import type {
  CreateProductRequest,
  InventoryItem,
  Movement,
  Role,
  StockLocation,
  TransactionResponse,
} from "../lib/types";

interface ScannerTransactionProps {
  token: string;
  role?: Role;
  locations: StockLocation[];
  locationsLoading: boolean;
  locationsError: string | null;
  onRetryLocations: () => void;
  onComplete: (response: TransactionResponse) => void | Promise<void>;
}

interface MovementReview {
  key: string;
  item: InventoryItem;
  location: StockLocation;
  balanceBefore: number;
  movement: Movement;
  quantity: number;
  note?: string;
}

interface ProductReview extends CreateProductRequest {
  key: string;
  location: StockLocation;
  imageFile: File | null;
}

type ScreenMode = "scan" | "new";

const MAX_ACQUISITION_COST_CENTAVOS = 1_000_000_000;
const MAX_PRODUCT_IMAGE_BYTES = 10_000_000;
const PRODUCT_IMAGE_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
]);

function newIdempotencyKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `stockpile-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function newInternalBarcode() {
  const time = Date.now().toString(36).toUpperCase();
  const random = Math.random().toString(36).slice(2, 7).toUpperCase();
  return `STP-${time}-${random}`;
}

function movementLabel(movement: Movement) {
  return movement === "stock_in" ? "Add stock" : "Remove stock";
}

function parsePesoToCentavos(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  if (!/^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?$/.test(trimmed)) {
    throw new Error("Enter a valid peso amount with up to two decimal places.");
  }

  const normalized = trimmed.replace(/,/g, "");
  const [pesoPart, centavoPart = ""] = normalized.split(".");
  const centavos =
    Number(pesoPart) * 100 + Number(centavoPart.padEnd(2, "0"));

  if (
    !Number.isSafeInteger(centavos) ||
    centavos > MAX_ACQUISITION_COST_CENTAVOS
  ) {
    throw new Error("Acquisition cost cannot exceed ₱10,000,000.00.");
  }
  return centavos;
}

function formatPhp(centavos: number) {
  const pesos = Math.floor(centavos / 100).toLocaleString("en-PH");
  const fraction = String(centavos % 100).padStart(2, "0");
  return `₱${pesos}.${fraction}`;
}

function locationLabel(location: StockLocation) {
  return location.path || location.name || location.code;
}

function locationOption(location: StockLocation) {
  const label = locationLabel(location);
  return label === location.code ? location.code : `${location.code} — ${label}`;
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

export default function ScannerTransaction({
  token,
  role = "operator",
  locations,
  locationsLoading,
  locationsError,
  onRetryLocations,
  onComplete,
}: ScannerTransactionProps) {
  const [mode, setMode] = useState<ScreenMode>("scan");
  const [barcode, setBarcode] = useState("");
  const [unknownBarcode, setUnknownBarcode] = useState<string | null>(null);
  const [item, setItem] = useState<InventoryItem | null>(null);
  const [movement, setMovement] = useState<Movement>("stock_in");
  const [locationCode, setLocationCode] = useState("");
  const [quantity, setQuantity] = useState("");
  const [note, setNote] = useState("");
  const [review, setReview] = useState<MovementReview | null>(null);

  const [newSku, setNewSku] = useState("");
  const [newBarcode, setNewBarcode] = useState("");
  const [newName, setNewName] = useState("");
  const [newCategory, setNewCategory] = useState("");
  const [newUnit, setNewUnit] = useState("roll");
  const [newVariant, setNewVariant] = useState("");
  const [newLocationCode, setNewLocationCode] = useState("");
  const [newQuantity, setNewQuantity] = useState("");
  const [newAcquisitionCostPhp, setNewAcquisitionCostPhp] = useState("");
  const [newImageFile, setNewImageFile] = useState<File | null>(null);
  const [newNote, setNewNote] = useState("");
  const [productReview, setProductReview] = useState<ProductReview | null>(null);

  const [resolving, setResolving] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [receipt, setReceipt] = useState<TransactionResponse | null>(null);
  const [receiptKind, setReceiptKind] = useState<"movement" | "product">("movement");
  const [message, setMessage] = useState<{
    tone: "error" | "success" | "info";
    text: string;
  } | null>(null);

  const submitLock = useRef(false);
  const scannerInput = useRef<HTMLInputElement>(null);
  const productNameInput = useRef<HTMLInputElement>(null);
  const productImageInput = useRef<HTMLInputElement>(null);
  const canCreateProducts = role === "admin";

  const stockableLocations = [...locations]
    .filter(
      (location) =>
        location.active &&
        location.stockable &&
        location.code !== "UNASSIGNED",
    )
    .sort((left, right) => {
      const leftReceiving = left.kind === "receiving" ? 0 : 1;
      const rightReceiving = right.kind === "receiving" ? 0 : 1;
      return (
        leftReceiving - rightReceiving ||
        locationLabel(left).localeCompare(locationLabel(right))
      );
    });

  const numericQuantity = Number(quantity);
  const positions = item?.positions ?? [];
  const visiblePositions = positions.filter((position) => position.quantity > 0);

  const activeLocations = stockableLocations.filter((location) => {
    if (movement === "stock_in") return true;
    return visiblePositions.some(
      (position) =>
        position.location_code === location.code && position.quantity > 0,
    );
  });

  const selectedLocation = activeLocations.find(
    (location) => location.code === locationCode,
  );
  const selectedPosition = selectedLocation
    ? positions.find(
        (position) => position.location_code === selectedLocation.code,
      )
    : undefined;
  const availableAtLocation = selectedPosition?.quantity ?? 0;
  const validQuantity =
    Number.isInteger(numericQuantity) && numericQuantity > 0;
  const wouldGoNegative =
    movement === "stock_out" &&
    validQuantity &&
    numericQuantity > availableAtLocation;
  const projectedStock =
    selectedLocation && validQuantity
      ? availableAtLocation +
        (movement === "stock_in" ? numericQuantity : -numericQuantity)
      : null;

  const selectedNewLocation = stockableLocations.find(
    (location) => location.code === newLocationCode,
  );
  const numericNewQuantity = Number(newQuantity);
  const validNewQuantity =
    Number.isInteger(numericNewQuantity) && numericNewQuantity > 0;

  function preferredReceivingLocation() {
    const receiving = stockableLocations.filter(
      (location) => location.kind === "receiving",
    );
    return receiving.length === 1 ? receiving[0].code : "";
  }

  function clearStatus() {
    setMessage(null);
    setReceipt(null);
  }

  function invalidateMovementReview() {
    setReview(null);
    clearStatus();
  }

  function invalidateProductReview() {
    setProductReview(null);
    clearStatus();
  }

  function selectLocationForItem(resolvedItem: InventoryItem) {
    const stockableCodes = new Set(
      stockableLocations.map((location) => location.code),
    );

    if (movement === "stock_in") {
      if (!stockableCodes.has(locationCode)) {
        setLocationCode(preferredReceivingLocation());
      }
      return;
    }

    const stockedLocations = (resolvedItem.positions ?? []).filter(
      (position) =>
        position.quantity > 0 && stockableCodes.has(position.location_code),
    );
    if (!stockedLocations.some((position) => position.location_code === locationCode)) {
      setLocationCode(
        stockedLocations.length === 1 ? stockedLocations[0].location_code : "",
      );
    }
  }

  function changeMovement(nextMovement: Movement) {
    setMovement(nextMovement);
    if (nextMovement === "stock_in") {
      if (!stockableLocations.some((location) => location.code === locationCode)) {
        setLocationCode(preferredReceivingLocation());
      }
    } else {
      const stockedLocations = visiblePositions.filter((position) =>
        stockableLocations.some(
          (location) => location.code === position.location_code,
        ),
      );
      if (!stockedLocations.some((position) => position.location_code === locationCode)) {
        setLocationCode(
          stockedLocations.length === 1
            ? stockedLocations[0].location_code
            : "",
        );
      }
    }
    invalidateMovementReview();
  }

  function switchMode(nextMode: ScreenMode, seedBarcode = "") {
    setMode(nextMode);
    setReceipt(null);
    setMessage(null);
    setReview(null);
    setProductReview(null);
    setUnknownBarcode(null);

    if (nextMode === "new") {
      if (seedBarcode) {
        resetProductForm();
        setNewBarcode(seedBarcode);
      }
      setNewLocationCode((current) => current || preferredReceivingLocation());
      window.setTimeout(() => productNameInput.current?.focus(), 0);
    } else {
      window.setTimeout(() => scannerInput.current?.focus(), 0);
    }
  }

  async function resolveBarcode(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = barcode.trim();
    setReceipt(null);
    setUnknownBarcode(null);

    if (!value) {
      setItem(null);
      setReview(null);
      setMessage({ tone: "error", text: "Scan or enter a barcode." });
      scannerInput.current?.focus();
      return;
    }

    setResolving(true);
    setItem(null);
    setReview(null);
    setQuantity("");
    setNote("");
    setMessage({ tone: "info", text: "Reading barcode…" });

    try {
      const response = await stockpileApi.barcode(token, value);
      setBarcode(response.barcode);
      setItem(response);
      selectLocationForItem(response);
      setMessage(
        response.active === false
          ? { tone: "error", text: "This product is archived." }
          : null,
      );
    } catch (error) {
      const missing = error instanceof ApiError && error.status === 404;
      setUnknownBarcode(missing ? value : null);
      setMessage({
        tone: "error",
        text: missing
          ? `Barcode ${value} is not registered.`
          : error instanceof Error
            ? error.message
            : "Barcode lookup failed.",
      });
    } finally {
      setResolving(false);
    }
  }

  function prepareMovement(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!item) {
      setMessage({ tone: "error", text: "Scan a valid barcode first." });
      return;
    }
    if (item.active === false) {
      setMessage({ tone: "error", text: "This product is archived." });
      return;
    }
    if (!selectedLocation) {
      setMessage({
        tone: "error",
        text:
          movement === "stock_out" && activeLocations.length === 0
            ? "No stock is available to remove."
            : "Choose a location.",
      });
      return;
    }
    if (!validQuantity) {
      setMessage({ tone: "error", text: "Enter a positive whole number." });
      return;
    }
    if (wouldGoNegative) {
      setMessage({
        tone: "error",
        text: `Only ${availableAtLocation.toLocaleString()} available at ${selectedLocation.code}.`,
      });
      return;
    }

    setReview({
      key: newIdempotencyKey(),
      item,
      location: selectedLocation,
      balanceBefore: availableAtLocation,
      movement,
      quantity: numericQuantity,
      note: note.trim() || undefined,
    });
    setMessage(null);
  }

  async function confirmMovement() {
    if (!review || submitLock.current) return;
    submitLock.current = true;
    setSubmitting(true);
    setMessage({ tone: "info", text: "Recording…" });

    try {
      const response = await stockpileApi.createTransaction(token, review.key, {
        barcode: review.item.barcode,
        movement_type: review.movement,
        quantity: review.quantity,
        location_code: review.location.code,
        reason: review.note,
        source: "scanner",
      });
      await onComplete(response);
      setItem(response.inventory);
      setQuantity("");
      setNote("");
      setReview(null);
      setReceiptKind("movement");
      setReceipt(response);
      setMessage(null);
    } catch (error) {
      setMessage({
        tone: "error",
        text: error instanceof Error ? error.message : "Transaction failed. Retry.",
      });
    } finally {
      submitLock.current = false;
      setSubmitting(false);
    }
  }

  function prepareProduct(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const sku = newSku.trim().toUpperCase();
    const productBarcode = newBarcode.trim();
    const productName = newName.trim();
    const category = newCategory.trim();
    const unit = newUnit.trim();

    if (!productName || !sku || !productBarcode || !category || !unit) {
      setMessage({ tone: "error", text: "Complete the required fields." });
      return;
    }
    if (!selectedNewLocation) {
      setMessage({ tone: "error", text: "Choose a location." });
      return;
    }
    if (!validNewQuantity) {
      setMessage({ tone: "error", text: "Enter a positive whole number." });
      return;
    }

    let acquisitionCost: number | null;
    try {
      acquisitionCost = parsePesoToCentavos(newAcquisitionCostPhp);
    } catch (error) {
      setMessage({
        tone: "error",
        text:
          error instanceof Error
            ? error.message
            : "Enter a valid acquisition cost.",
      });
      return;
    }

    setProductReview({
      key: newIdempotencyKey(),
      sku,
      barcode: productBarcode,
      product_name: productName,
      category,
      unit,
      variant: newVariant.trim() || undefined,
      location_code: selectedNewLocation.code,
      quantity: numericNewQuantity,
      reason: newNote.trim() || undefined,
      ...(acquisitionCost !== null
        ? { acquisition_cost_centavos: acquisitionCost }
        : {}),
      location: selectedNewLocation,
      imageFile: newImageFile,
    });
    setMessage(null);
  }

  function selectProductImage(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0] ?? null;

    if (!file) return;

    if (file.size === 0) {
      event.currentTarget.value = "";
      setNewImageFile(null);
      setProductReview(null);
      setMessage({ tone: "error", text: "Choose a non-empty image." });
      return;
    }

    if (file.type && !PRODUCT_IMAGE_TYPES.has(file.type)) {
      event.currentTarget.value = "";
      setNewImageFile(null);
      setProductReview(null);
      setMessage({ tone: "error", text: "Use a JPEG, PNG, or WebP image." });
      return;
    }

    if (file.size > MAX_PRODUCT_IMAGE_BYTES) {
      event.currentTarget.value = "";
      setNewImageFile(null);
      setProductReview(null);
      setMessage({ tone: "error", text: "Keep the photo under 10 MB." });
      return;
    }

    setNewImageFile(file);
    invalidateProductReview();
  }

  function clearProductImage() {
    setNewImageFile(null);
    if (productImageInput.current) productImageInput.current.value = "";
    invalidateProductReview();
  }

  async function confirmProduct() {
    if (!productReview || submitLock.current) return;
    submitLock.current = true;
    setSubmitting(true);
    setMessage({ tone: "info", text: "Recording…" });

    try {
      const {
        key,
        location: _location,
        imageFile,
        ...payload
      } = productReview;
      let response = await stockpileApi.createProduct(token, key, payload);
      const warnings: string[] = [];

      if (imageFile) {
        try {
          const inventory = await stockpileApi.uploadProductImage(
            token,
            response.inventory.sku,
            imageFile,
          );
          response = { ...response, inventory };
        } catch {
          warnings.push("Add the photo from Inventory.");
        }
      }

      try {
        await onComplete(response);
      } catch {
        warnings.push("Refresh Inventory to see the latest stock.");
      }

      setProductReview(null);
      setReceiptKind("product");
      setReceipt(response);
      setMessage(
        warnings.length
          ? { tone: "info", text: `Item saved. ${warnings.join(" ")}` }
          : null,
      );
    } catch (error) {
      setMessage({
        tone: "error",
        text: error instanceof Error ? error.message : "Product could not be saved.",
      });
    } finally {
      submitLock.current = false;
      setSubmitting(false);
    }
  }

  function resetProductForm() {
    setNewSku("");
    setNewBarcode("");
    setNewName("");
    setNewCategory("");
    setNewUnit("roll");
    setNewVariant("");
    setNewLocationCode(preferredReceivingLocation());
    setNewQuantity("");
    setNewAcquisitionCostPhp("");
    setNewImageFile(null);
    if (productImageInput.current) productImageInput.current.value = "";
    setNewNote("");
    setProductReview(null);
  }

  function scanNext() {
    setMode("scan");
    setBarcode("");
    setUnknownBarcode(null);
    setItem(null);
    setQuantity("");
    setNote("");
    setReview(null);
    setReceipt(null);
    setMessage(null);
    resetProductForm();
    window.setTimeout(() => scannerInput.current?.focus(), 0);
  }

  return (
    <section className="panel scanner-panel" aria-labelledby="scanner-title">
      <div className="panel-heading scanner-heading">
        <h2 id="scanner-title">Stock</h2>

        {canCreateProducts && !receipt && (
          <div className="segmented-control scanner-mode" aria-label="Stock entry mode">
            <label className={mode === "scan" ? "selected" : ""}>
              <input
                type="radio"
                name="scanner-mode"
                checked={mode === "scan"}
                onChange={() => switchMode("scan")}
              />
              Scan
            </label>
            <label className={mode === "new" ? "selected" : ""}>
              <input
                type="radio"
                name="scanner-mode"
                checked={mode === "new"}
                onChange={() => switchMode("new")}
              />
              New item
            </label>
          </div>
        )}
      </div>

      {message && (
        <div
          className={`alert alert-${message.tone}`}
          role={message.tone === "error" ? "alert" : "status"}
          aria-live="polite"
        >
          {message.text}
        </div>
      )}

      {receipt ? (
        <>
          <article className="transaction-receipt" aria-label="Transaction receipt">
            <header>
              <strong>
                {receipt.replayed
                  ? "Already recorded"
                  : receiptKind === "product"
                    ? "Item received"
                    : "Recorded"}
              </strong>
              <code title={receipt.data.id}>#{receipt.data.id.slice(0, 8)}</code>
            </header>

            <dl>
              <div>
                <dt>Item</dt>
                <dd>{receipt.data.product_name}</dd>
              </div>
              <div>
                <dt>Change</dt>
                <dd>
                  {receipt.data.quantity_delta > 0 ? "+" : ""}
                  {receipt.data.quantity_delta.toLocaleString()}
                </dd>
              </div>
              <div>
                <dt>Location</dt>
                <dd>{receipt.data.location_path || receipt.data.location_name}</dd>
              </div>
              <div>
                <dt>At location</dt>
                <dd>
                  {receipt.data.location_balance_before.toLocaleString()} →{" "}
                  {receipt.data.location_balance_after.toLocaleString()}
                </dd>
              </div>
              <div>
                <dt>Total</dt>
                <dd>
                  {receipt.data.balance_before.toLocaleString()} →{" "}
                  {receipt.data.balance_after.toLocaleString()}
                </dd>
              </div>
              <div>
                <dt>By</dt>
                <dd>{receipt.data.actor_name}</dd>
              </div>
              <div>
                <dt>Time</dt>
                <dd>{formatTime(receipt.data.created_at)}</dd>
              </div>
            </dl>
          </article>

          <button
            type="button"
            className="button button-primary button-wide"
            onClick={scanNext}
          >
            Next scan
          </button>
        </>
      ) : mode === "new" && canCreateProducts ? (
        <div className="transaction-workspace">
          <form className="movement-form product-create-form" onSubmit={prepareProduct} noValidate>
            <div className="form-grid">
              <div>
                <label htmlFor="new-barcode">Barcode</label>
                <div className="scanner-row">
                  <input
                    id="new-barcode"
                    value={newBarcode}
                    onChange={(event) => {
                      setNewBarcode(event.target.value);
                      invalidateProductReview();
                    }}
                    autoComplete="off"
                    spellCheck={false}
                    placeholder="Scan existing code"
                    disabled={submitting}
                  />
                  <button
                    type="button"
                    className="button button-secondary"
                    onClick={() => {
                      setNewBarcode(newInternalBarcode());
                      invalidateProductReview();
                    }}
                    disabled={submitting}
                  >
                    Create code
                  </button>
                </div>
              </div>

              <div>
                <label htmlFor="new-sku">SKU</label>
                <input
                  id="new-sku"
                  value={newSku}
                  onChange={(event) => {
                    setNewSku(event.target.value.toUpperCase());
                    invalidateProductReview();
                  }}
                  placeholder="TXT-POP-WHT-60"
                  autoComplete="off"
                  spellCheck={false}
                  disabled={submitting}
                />
              </div>
            </div>

            <div className="form-grid">
              <div>
                <label htmlFor="new-name">Item</label>
                <input
                  ref={productNameInput}
                  id="new-name"
                  value={newName}
                  onChange={(event) => {
                    setNewName(event.target.value);
                    invalidateProductReview();
                  }}
                  placeholder="Cotton Poplin 60 in"
                  disabled={submitting}
                />
              </div>
              <div>
                <label htmlFor="new-variant">
                  Variant <span className="optional">optional</span>
                </label>
                <input
                  id="new-variant"
                  value={newVariant}
                  onChange={(event) => {
                    setNewVariant(event.target.value);
                    invalidateProductReview();
                  }}
                  placeholder="White · 50 m roll"
                  disabled={submitting}
                />
              </div>
            </div>

            <div className="form-grid">
              <div>
                <label htmlFor="new-category">Category</label>
                <input
                  id="new-category"
                  list="stockpile-categories"
                  value={newCategory}
                  onChange={(event) => {
                    setNewCategory(event.target.value);
                    invalidateProductReview();
                  }}
                  placeholder="Cotton Wovens"
                  disabled={submitting}
                />
                <datalist id="stockpile-categories">
                  <option value="Cotton Wovens" />
                  <option value="Uniform & Shirting" />
                  <option value="Knits & Stretch" />
                  <option value="Formal & Occasion" />
                  <option value="Lining & Interfacing" />
                  <option value="Notions" />
                  <option value="Packing & Labels" />
                </datalist>
              </div>
              <div>
                <label htmlFor="new-unit">Unit</label>
                <input
                  id="new-unit"
                  list="stockpile-units"
                  value={newUnit}
                  onChange={(event) => {
                    setNewUnit(event.target.value);
                    invalidateProductReview();
                  }}
                  disabled={submitting}
                />
                <datalist id="stockpile-units">
                  <option value="roll" />
                  <option value="bolt" />
                  <option value="meter" />
                  <option value="kilogram" />
                  <option value="box" />
                  <option value="pack" />
                  <option value="piece" />
                </datalist>
              </div>
            </div>

            <div className="form-grid">
              <div>
                <label htmlFor="new-location">Location</label>
                {locationsError ? (
                  <div className="inline-recovery" role="alert">
                    <span>{locationsError}</span>
                    <button type="button" className="text-button" onClick={onRetryLocations}>
                      Retry
                    </button>
                  </div>
                ) : (
                  <select
                    id="new-location"
                    value={selectedNewLocation?.code ?? ""}
                    onChange={(event) => {
                      setNewLocationCode(event.target.value);
                      invalidateProductReview();
                    }}
                    disabled={locationsLoading || submitting}
                  >
                    <option value="">
                      {locationsLoading ? "Loading…" : "Choose location"}
                    </option>
                    {stockableLocations.map((location) => (
                      <option key={location.code} value={location.code}>
                        {locationOption(location)}
                      </option>
                    ))}
                  </select>
                )}
              </div>
              <div>
                <label htmlFor="new-quantity">Opening quantity</label>
                <input
                  id="new-quantity"
                  type="number"
                  min="1"
                  max="1000000"
                  step="1"
                  inputMode="numeric"
                  value={newQuantity}
                  onChange={(event) => {
                    setNewQuantity(event.target.value);
                    invalidateProductReview();
                  }}
                  placeholder="0"
                  disabled={submitting}
                />
              </div>
            </div>

            <div className="form-grid">
              <div>
                <label htmlFor="new-acquisition-cost">
                  Acquisition cost <span className="optional">optional</span>
                </label>
                <div className="currency-input">
                  <span aria-hidden="true">₱</span>
                  <input
                    id="new-acquisition-cost"
                    type="text"
                    inputMode="decimal"
                    value={newAcquisitionCostPhp}
                    onChange={(event) => {
                      setNewAcquisitionCostPhp(event.target.value);
                      invalidateProductReview();
                    }}
                    placeholder="0.00"
                    autoComplete="off"
                    disabled={submitting}
                  />
                </div>
              </div>

              <div>
                <label htmlFor="new-product-image">
                  Photo <span className="optional">optional</span>
                </label>
                <div className="button-row">
                  {newImageFile && (
                    <span className="cell-subtext" title={newImageFile.name}>
                      {newImageFile.name}
                    </span>
                  )}
                  {newImageFile && (
                    <button
                      type="button"
                      className="text-button"
                      onClick={clearProductImage}
                      disabled={submitting}
                    >
                      Clear
                    </button>
                  )}
                  <button
                    type="button"
                    className="button button-secondary"
                    onClick={() => productImageInput.current?.click()}
                    disabled={submitting}
                  >
                    {newImageFile ? "Replace" : "Choose photo"}
                  </button>
                  <input
                    ref={productImageInput}
                    id="new-product-image"
                    className="sr-only"
                    type="file"
                    accept="image/jpeg,image/png,image/webp"
                    onChange={selectProductImage}
                    disabled={submitting}
                  />
                </div>
              </div>
            </div>

            <div>
              <label htmlFor="new-note">
                Note <span className="optional">optional</span>
              </label>
              <input
                id="new-note"
                value={newNote}
                maxLength={500}
                onChange={(event) => {
                  setNewNote(event.target.value);
                  invalidateProductReview();
                }}
                placeholder="Delivery reference"
                disabled={submitting}
              />
            </div>

            {!productReview && (
              <button
                type="submit"
                className="button button-primary button-wide"
                disabled={
                  !newSku.trim() ||
                  !newBarcode.trim() ||
                  !newName.trim() ||
                  !newCategory.trim() ||
                  !newUnit.trim() ||
                  !selectedNewLocation ||
                  !validNewQuantity ||
                  submitting
                }
              >
                Review
              </button>
            )}
          </form>

          {productReview && (
            <article className="review-card" aria-labelledby="new-review-title">
              <h3 id="new-review-title">Confirm new item</h3>
              <dl className="review-grid">
                <div>
                  <dt>Item</dt>
                  <dd>{productReview.product_name}</dd>
                </div>
                <div>
                  <dt>SKU</dt>
                  <dd>{productReview.sku}</dd>
                </div>
                <div>
                  <dt>Location</dt>
                  <dd>{locationOption(productReview.location)}</dd>
                </div>
                <div>
                  <dt>Opening stock</dt>
                  <dd>
                    {productReview.quantity.toLocaleString()} {productReview.unit}
                  </dd>
                </div>
                {productReview.acquisition_cost_centavos !== undefined &&
                  productReview.acquisition_cost_centavos !== null && (
                    <div>
                      <dt>Cost</dt>
                      <dd>
                        {formatPhp(productReview.acquisition_cost_centavos)} each
                      </dd>
                    </div>
                  )}
                {productReview.imageFile && (
                  <div>
                    <dt>Photo</dt>
                    <dd>{productReview.imageFile.name}</dd>
                  </div>
                )}
              </dl>
              <div className="button-row">
                <button
                  type="button"
                  className="button button-secondary"
                  onClick={() => setProductReview(null)}
                  disabled={submitting}
                >
                  Edit
                </button>
                <button
                  type="button"
                  className="button button-primary"
                  onClick={confirmProduct}
                  disabled={submitting}
                >
                  {submitting ? "Recording…" : "Confirm"}
                </button>
              </div>
            </article>
          )}
        </div>
      ) : (
        <>
          <form className="scanner-form" onSubmit={resolveBarcode} aria-busy={resolving}>
            <label htmlFor="barcode">Barcode</label>
            <div className="scanner-row">
              <input
                ref={scannerInput}
                id="barcode"
                name="barcode"
                value={barcode}
                onChange={(event) => {
                  setBarcode(event.target.value);
                  setUnknownBarcode(null);
                  setItem(null);
                  setReview(null);
                  clearStatus();
                }}
                placeholder="Scan or type"
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                enterKeyHint="search"
                disabled={resolving || submitting}
                autoFocus
              />
              <button
                type="submit"
                className="button button-secondary"
                disabled={resolving || submitting}
              >
                {resolving ? "Reading…" : "Find"}
              </button>
            </div>
          </form>

          {unknownBarcode && canCreateProducts && (
            <button
              type="button"
              className="text-button unknown-product-action"
              onClick={() => switchMode("new", unknownBarcode)}
            >
              Add this item
            </button>
          )}

          {item && (
            <div className="transaction-workspace">
              <article className="resolved-item">
                <div>
                  <h3>{item.product_name}</h3>
                  <p className="muted">
                    {item.sku} · {item.barcode}
                    {item.variant ? ` · ${item.variant}` : ""}
                  </p>
                </div>
                <div className="stock-number">
                  <span>On hand</span>
                  <strong>{item.current_stock.toLocaleString()}</strong>
                  {item.unit && <small>{item.unit}</small>}
                </div>
              </article>

              <div className="position-list" aria-label="Stock by location">
                {visiblePositions.length ? (
                  visiblePositions.map((position) => (
                    <div key={position.location_code}>
                      <span>
                        <strong>{position.location_code}</strong>
                        <small>{position.location_path || position.location_name}</small>
                      </span>
                      <b>{position.quantity.toLocaleString()}</b>
                    </div>
                  ))
                ) : (
                  <p>No stock recorded.</p>
                )}
              </div>

              <form className="movement-form" onSubmit={prepareMovement} noValidate>
                <fieldset>
                  <legend>Action</legend>
                  <div className="segmented-control">
                    {(["stock_in", "stock_out"] as Movement[]).map((choice) => (
                      <label key={choice} className={movement === choice ? "selected" : ""}>
                        <input
                          type="radio"
                          name="movement"
                          value={choice}
                          checked={movement === choice}
                          onChange={() => changeMovement(choice)}
                          disabled={submitting || item.active === false}
                        />
                        {movementLabel(choice)}
                      </label>
                    ))}
                  </div>
                </fieldset>

                <div className="location-field">
                  <label htmlFor="movement-location">Location</label>
                  {locationsError ? (
                    <div className="inline-recovery" role="alert">
                      <span>{locationsError}</span>
                      <button type="button" className="text-button" onClick={onRetryLocations}>
                        Retry
                      </button>
                    </div>
                  ) : (
                    <select
                      id="movement-location"
                      value={selectedLocation?.code ?? ""}
                      onChange={(event) => {
                        setLocationCode(event.target.value);
                        invalidateMovementReview();
                      }}
                      disabled={
                        locationsLoading || submitting || item.active === false
                      }
                    >
                      <option value="">
                        {locationsLoading
                          ? "Loading…"
                          : activeLocations.length
                            ? "Choose location"
                            : "No stock available"}
                      </option>
                      {activeLocations.map((location) => (
                        <option key={location.code} value={location.code}>
                          {locationOption(location)}
                        </option>
                      ))}
                    </select>
                  )}

                  {selectedLocation && (
                    <div className="location-availability">
                      <span>{locationLabel(selectedLocation)}</span>
                      <strong>{availableAtLocation.toLocaleString()} here</strong>
                    </div>
                  )}
                </div>

                <div className="form-grid">
                  <div>
                    <label htmlFor="quantity">Quantity</label>
                    <input
                      id="quantity"
                      name="quantity"
                      type="number"
                      min="1"
                      max={movement === "stock_out" ? availableAtLocation : undefined}
                      step="1"
                      inputMode="numeric"
                      value={quantity}
                      onChange={(event) => {
                        setQuantity(event.target.value);
                        invalidateMovementReview();
                      }}
                      placeholder="0"
                      disabled={submitting || item.active === false}
                    />
                  </div>
                  <div>
                    <label htmlFor="note">
                      Note <span className="optional">optional</span>
                    </label>
                    <input
                      id="note"
                      name="note"
                      value={note}
                      maxLength={500}
                      onChange={(event) => {
                        setNote(event.target.value);
                        invalidateMovementReview();
                      }}
                      placeholder="Delivery, sale, return…"
                      disabled={submitting || item.active === false}
                    />
                  </div>
                </div>

                <div className={`projection ${wouldGoNegative ? "projection-error" : ""}`}>
                  <span>{wouldGoNegative ? "Insufficient stock" : "Location after"}</span>
                  <strong>
                    {wouldGoNegative
                      ? `${availableAtLocation.toLocaleString()} max`
                      : projectedStock !== null
                        ? projectedStock.toLocaleString()
                        : "—"}
                  </strong>
                </div>

                {!review && (
                  <button
                    type="submit"
                    className="button button-primary button-wide"
                    disabled={
                      item.active === false ||
                      !selectedLocation ||
                      !validQuantity ||
                      wouldGoNegative ||
                      submitting
                    }
                  >
                    Review
                  </button>
                )}
              </form>

              {review && (
                <article className="review-card" aria-labelledby="review-title">
                  <h3 id="review-title">Confirm</h3>
                  <dl className="review-grid">
                    <div>
                      <dt>Item</dt>
                      <dd>{review.item.product_name}</dd>
                    </div>
                    <div>
                      <dt>Action</dt>
                      <dd>{movementLabel(review.movement)}</dd>
                    </div>
                    <div>
                      <dt>Location</dt>
                      <dd>{locationOption(review.location)}</dd>
                    </div>
                    <div>
                      <dt>Quantity</dt>
                      <dd>{review.quantity.toLocaleString()}</dd>
                    </div>
                    <div>
                      <dt>At location</dt>
                      <dd>
                        {review.balanceBefore.toLocaleString()} →{" "}
                        {(
                          review.balanceBefore +
                          (review.movement === "stock_in"
                            ? review.quantity
                            : -review.quantity)
                        ).toLocaleString()}
                      </dd>
                    </div>
                  </dl>
                  <div className="button-row">
                    <button
                      type="button"
                      className="button button-secondary"
                      onClick={() => setReview(null)}
                      disabled={submitting}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="button button-primary"
                      onClick={confirmMovement}
                      disabled={submitting}
                    >
                      {submitting ? "Recording…" : "Confirm"}
                    </button>
                  </div>
                </article>
              )}

              <button type="button" className="text-button" onClick={scanNext} disabled={submitting}>
                Clear item
              </button>
            </div>
          )}
        </>
      )}
    </section>
  );
}
