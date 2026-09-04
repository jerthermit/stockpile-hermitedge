"use client";

import {
  ChangeEvent,
  FormEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { stockpileApi } from "../lib/api";
import type {
  InventoryItem,
  InventoryPosition,
  Role,
  StockLocation,
  UpdateProductRequest,
} from "../lib/types";

interface InventoryTableProps {
  items: InventoryItem[];
  locations: StockLocation[];
  token: string;
  role: Role;
  loading: boolean;
  error: string | null;
  onRetry: () => void | Promise<void>;
  onChanged: () => void | Promise<void>;
  embedded?: boolean;
}

interface ProductEditor {
  item: InventoryItem;
  barcode: string;
  productName: string;
  category: string;
  unit: string;
  variant: string;
  acquisitionCostPhp: string;
}

const PAGE_SIZE = 12;
const UNASSIGNED = "UNASSIGNED";
const MAX_PRODUCT_IMAGE_BYTES = 10_000_000;
const DEMO_PRODUCT_IMAGE_SKUS = new Set([
  "BAG-XL",
  "PKG-BAG-M",
  "TAPE-048",
  "TXT-BC-WHT-60",
  "TXT-CHF-WHT-60",
  "TXT-CNV-NAT-60",
  "TXT-POP-BLK-60",
  "TXT-POP-WHT-60",
  "TXT-SAT-BLK-60",
  "TXT-SAT-CHP-60",
  "TXT-SPX-BLK-60",
  "TXT-TWL-KHK-60",
]);
const MAX_ACQUISITION_COST_CENTAVOS = 1_000_000_000;

function positionLabel(position: InventoryPosition) {
  if (position.location_code === UNASSIGNED) return "Location not recorded";
  const name =
    position.location_path || position.location_name || position.location_code;
  return name === position.location_code
    ? position.location_code
    : `${position.location_code} — ${name}`;
}

function locationLabel(location: StockLocation) {
  if (location.code === UNASSIGNED) return "Location not recorded";
  const name = location.path || location.name || location.code;
  return name === location.code
    ? location.code
    : `${location.code} — ${name}`;
}

function statusLabel(status: string) {
  return status.replace(/_/g, " ").toLowerCase();
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

function formatDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("en-PH", {
        day: "numeric",
        month: "short",
        timeZone: "Asia/Manila",
      }).format(date);
}

function positionShortName(position: InventoryPosition) {
  if (position.location_code === UNASSIGNED) {
    return "Location not recorded";
  }
  const name = position.location_name || position.location_path;
  return name && name !== position.location_code ? name : null;
}

function productMark(item: InventoryItem) {
  const source = item.variant || item.product_name;
  const words = source
    .replace(/[^A-Za-z0-9 ]/g, " ")
    .split(/\s+/)
    .filter(Boolean);
  return words
    .slice(0, 2)
    .map((word) => word[0])
    .join("")
    .toUpperCase();
}

function centavosToPesoInput(value: number | null | undefined) {
  if (value == null) return "";
  const pesos = Math.floor(value / 100);
  const centavos = String(value % 100).padStart(2, "0");
  return `${pesos}.${centavos}`;
}

function formatPhp(value: number) {
  const pesos = Math.floor(value / 100).toLocaleString("en-PH");
  const centavos = String(value % 100).padStart(2, "0");
  return `₱${pesos}.${centavos}`;
}

function parsePesoToCentavos(raw: string): number | null {
  const value = raw.trim();
  if (!value) return null;
  if (!/^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?$/.test(value)) {
    throw new Error("Enter a valid peso amount with up to two decimal places.");
  }

  const normalized = value.replace(/,/g, "");
  const [pesos, fraction = ""] = normalized.split(".");
  const centavos = Number(pesos) * 100 + Number(fraction.padEnd(2, "0"));
  if (
    !Number.isSafeInteger(centavos) ||
    centavos > MAX_ACQUISITION_COST_CENTAVOS
  ) {
    throw new Error("Acquisition cost cannot exceed ₱10,000,000.00.");
  }
  return centavos;
}

function ProductThumbnail({
  item,
  token,
  large = false,
}: {
  item: InventoryItem;
  token: string;
  large?: boolean;
}) {
  const fallbackSource = DEMO_PRODUCT_IMAGE_SKUS.has(item.sku)
    ? `/${item.sku}.jpg`
    : null;
  const [source, setSource] = useState<string | null>(fallbackSource);

  useEffect(() => {
    let active = true;
    let objectUrl: string | null = null;
    setSource(fallbackSource);

    if (!item.image_url) {
      return () => {
        active = false;
      };
    }

    stockpileApi
      .productImage(token, item.image_url)
      .then((blob) => {
        if (!active) return;
        objectUrl = URL.createObjectURL(blob);
        setSource(objectUrl);
      })
      .catch(() => {
        if (active) setSource(fallbackSource);
      });

    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [fallbackSource, item.image_url, token]);

  return (
    <span
      className={`product-thumbnail${large ? " product-thumbnail-large" : ""}${
        source ? " has-image" : " product-thumbnail-fallback"
      }`}
      aria-hidden="true"
    >
      {source ? (
        <img src={source} alt="" onError={() => setSource(null)} />
      ) : (
        productMark(item) || "ST"
      )}
    </span>
  );
}

export default function InventoryTable({
  items,
  locations,
  token,
  role,
  loading,
  error,
  onRetry,
  onChanged,
  embedded = false,
}: InventoryTableProps) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("all");
  const [locationCode, setLocationCode] = useState("all");
  const [scope, setScope] = useState<"active" | "archived" | "all">("active");
  const [page, setPage] = useState(0);
  const [editor, setEditor] = useState<ProductEditor | null>(null);
  const [saving, setSaving] = useState(false);
  const [imageAction, setImageAction] = useState<"upload" | "remove" | null>(null);
  const [imageRemoveArmed, setImageRemoveArmed] = useState(false);
  const [imageStatus, setImageStatus] = useState("");
  const [editorError, setEditorError] = useState<string | null>(null);
  const [archiveArmed, setArchiveArmed] = useState(false);
  const imageInputRef = useRef<HTMLInputElement | null>(null);
  const busy = saving || imageAction !== null;

  const categories = useMemo(
    () =>
      [...new Set(items.map((item) => item.category).filter(Boolean))].sort(
        (left, right) => left.localeCompare(right),
      ),
    [items],
  );

  const locationOptions = useMemo(() => {
    const knownLocations = new Map(
      locations.map((location) => [location.code, locationLabel(location)]),
    );
    const options = new Map<string, string>();

    items.forEach((item) => {
      (item.positions ?? []).forEach((position) => {
        if (position.quantity <= 0) return;
        options.set(
          position.location_code,
          knownLocations.get(position.location_code) || positionLabel(position),
        );
      });
    });

    return [...options.entries()]
      .map(([code, label]) => ({ code, label }))
      .sort((left, right) => {
        if (left.code === UNASSIGNED) return 1;
        if (right.code === UNASSIGNED) return -1;
        return left.label.localeCompare(right.label);
      });
  }, [items, locations]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();

    return items.filter((item) => {
      const active = item.active !== false;
      if (role !== "admin" && !active) return false;
      if (role === "admin" && scope === "active" && !active) return false;
      if (role === "admin" && scope === "archived" && active) return false;
      if (category !== "all" && item.category !== category) return false;

      const positions = item.positions ?? [];
      if (
        locationCode !== "all" &&
        !positions.some(
          (position) =>
            position.location_code === locationCode && position.quantity > 0,
        )
      ) {
        return false;
      }

      if (!needle) return true;
      return [
        item.product_name,
        item.sku,
        item.barcode,
        item.category,
        item.unit,
        item.variant,
        ...positions.flatMap((position) => [
          position.location_code,
          position.location_name,
          position.location_path,
        ]),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
  }, [category, items, locationCode, query, role, scope]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const safePage = Math.min(page, pageCount - 1);
  const pageItems = filtered.slice(
    safePage * PAGE_SIZE,
    safePage * PAGE_SIZE + PAGE_SIZE,
  );
  const firstVisible = filtered.length ? safePage * PAGE_SIZE + 1 : 0;
  const lastVisible = Math.min((safePage + 1) * PAGE_SIZE, filtered.length);

  function openEditor(item: InventoryItem) {
    setEditor({
      item,
      barcode: item.barcode,
      productName: item.product_name,
      category: item.category,
      unit: item.unit || "unit",
      variant: item.variant || "",
      acquisitionCostPhp: centavosToPesoInput(
        item.acquisition_cost_centavos,
      ),
    });
    setEditorError(null);
    setArchiveArmed(false);
    setImageRemoveArmed(false);
    setImageStatus("");
  }

  function closeEditor() {
    if (busy) return;
    setEditor(null);
    setEditorError(null);
    setArchiveArmed(false);
    setImageRemoveArmed(false);
    setImageStatus("");
  }

  function updateEditor(change: Partial<ProductEditor>) {
    setEditor((current) => (current ? { ...current, ...change } : current));
    setEditorError(null);
    setArchiveArmed(false);
    setImageRemoveArmed(false);
    setImageStatus("");
  }

  async function saveProduct(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editor || busy) return;

    const barcode = editor.barcode.trim();
    const productName = editor.productName.trim();
    const categoryValue = editor.category.trim();
    const unit = editor.unit.trim();
    const variant = editor.variant.trim();
    if (!barcode || !productName || !categoryValue || !unit) {
      setEditorError("Complete the required fields.");
      return;
    }

    let acquisitionCostCentavos: number | null;
    try {
      acquisitionCostCentavos = parsePesoToCentavos(editor.acquisitionCostPhp);
    } catch (costError) {
      setEditorError(
        costError instanceof Error
          ? costError.message
          : "Enter a valid acquisition cost.",
      );
      return;
    }

    const input: UpdateProductRequest = {};
    if (barcode !== editor.item.barcode) input.barcode = barcode;
    if (productName !== editor.item.product_name) input.product_name = productName;
    if (categoryValue !== editor.item.category) input.category = categoryValue;
    if (unit !== (editor.item.unit || "unit")) input.unit = unit;
    if (variant !== (editor.item.variant || "")) input.variant = variant || null;
    if (
      acquisitionCostCentavos !==
      (editor.item.acquisition_cost_centavos ?? null)
    ) {
      input.acquisition_cost_centavos = acquisitionCostCentavos;
    }

    if (!Object.keys(input).length) {
      closeEditor();
      return;
    }

    setSaving(true);
    setEditorError(null);
    try {
      await stockpileApi.updateProduct(token, editor.item.sku, input);
      await onChanged();
      setEditor(null);
    } catch (saveError) {
      setEditorError(
        saveError instanceof Error ? saveError.message : "Product could not be saved.",
      );
    } finally {
      setSaving(false);
    }
  }

  async function changeArchiveState() {
    if (!editor || busy) return;
    const active = editor.item.active !== false;

    if (active && !archiveArmed) {
      setArchiveArmed(true);
      return;
    }

    setSaving(true);
    setEditorError(null);
    try {
      await stockpileApi.updateProduct(token, editor.item.sku, {
        active: !active,
      });
      await onChanged();
      setEditor(null);
      setArchiveArmed(false);
    } catch (archiveError) {
      setEditorError(
        archiveError instanceof Error
          ? archiveError.message
          : "Product could not be updated.",
      );
    } finally {
      setSaving(false);
    }
  }

  async function uploadProductImage(
    event: ChangeEvent<HTMLInputElement>,
  ) {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!editor || !file || busy) return;

    if (
      file.type &&
      !["image/jpeg", "image/png", "image/webp"].includes(file.type)
    ) {
      setEditorError("Choose a JPEG, PNG, or WebP image.");
      return;
    }
    if (file.size > MAX_PRODUCT_IMAGE_BYTES) {
      setEditorError("Choose an image no larger than 10 MB.");
      return;
    }

    const sku = editor.item.sku;
    setImageAction("upload");
    setImageRemoveArmed(false);
    setImageStatus("");
    setEditorError(null);
    try {
      const updated = await stockpileApi.uploadProductImage(
        token,
        sku,
        file,
      );
      setEditor((current) =>
        current && current.item.sku === sku
          ? { ...current, item: updated }
          : current,
      );
      try {
        await onChanged();
        setImageStatus("Photo updated.");
      } catch {
        setEditorError("Photo saved. Reload inventory to refresh the table.");
        setImageStatus("Photo updated.");
      }
    } catch (uploadError) {
      setEditorError(
        uploadError instanceof Error
          ? uploadError.message
          : "Photo could not be saved.",
      );
    } finally {
      setImageAction(null);
    }
  }

  async function removeProductImage() {
    if (!editor || !editor.item.image_url || busy) return;
    if (!imageRemoveArmed) {
      setImageRemoveArmed(true);
      setEditorError(null);
      setImageStatus("");
      return;
    }

    const sku = editor.item.sku;
    setImageAction("remove");
    setImageStatus("");
    setEditorError(null);
    try {
      const updated = await stockpileApi.deleteProductImage(
        token,
        sku,
      );
      setEditor((current) =>
        current && current.item.sku === sku
          ? { ...current, item: updated }
          : current,
      );
      try {
        await onChanged();
        setImageStatus("Photo removed.");
      } catch {
        setEditorError("Photo removed. Reload inventory to refresh the table.");
        setImageStatus("Photo removed.");
      }
    } catch (removeError) {
      setEditorError(
        removeError instanceof Error
          ? removeError.message
          : "Photo could not be removed.",
      );
    } finally {
      setImageAction(null);
      setImageRemoveArmed(false);
    }
  }

  const pageControls = (
    <div className="table-page-controls" aria-label="Inventory pages">
      <span>
        {firstVisible}–{lastVisible} / {filtered.length.toLocaleString("en-PH")}
      </span>
      <button
        type="button"
        className="icon-page-button"
        onClick={() => setPage(Math.max(0, safePage - 1))}
        disabled={safePage === 0}
        aria-label="Previous inventory page"
        title="Previous page"
      >
        ‹
      </button>
      <button
        type="button"
        className="icon-page-button"
        onClick={() => setPage(Math.min(pageCount - 1, safePage + 1))}
        disabled={safePage >= pageCount - 1}
        aria-label="Next inventory page"
        title="Next page"
      >
        ›
      </button>
    </div>
  );

  const tools = (
    <div className="panel-tools inventory-tools">
      <label className="search-field inventory-search">
        <span className="sr-only">Search inventory</span>
        <input
          type="search"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setPage(0);
          }}
          placeholder="Search"
          autoComplete="off"
          spellCheck={false}
          aria-label="Search inventory"
        />
      </label>

      <label className="category-field">
        <span className="sr-only">Filter by category</span>
        <select
          value={category}
          onChange={(event) => {
            setCategory(event.target.value);
            setPage(0);
          }}
          aria-label="Filter by category"
        >
          <option value="all">All categories</option>
          {categories.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </label>

      <label className="location-filter-field">
        <span className="sr-only">Filter by location</span>
        <select
          value={locationCode}
          onChange={(event) => {
            setLocationCode(event.target.value);
            setPage(0);
          }}
          aria-label="Filter by location"
        >
          <option value="all">All locations</option>
          {locationOptions.map((option) => (
            <option key={option.code} value={option.code}>
              {option.label}
            </option>
          ))}
        </select>
      </label>

      {role === "admin" && (
        <label className="status-filter-field">
          <span className="sr-only">Filter active or archived products</span>
          <select
            value={scope}
            onChange={(event) => {
              setScope(event.target.value as "active" | "archived" | "all");
              setPage(0);
            }}
            aria-label="Filter active or archived products"
          >
            <option value="active">Active</option>
            <option value="archived">Archived</option>
            <option value="all">All items</option>
          </select>
        </label>
      )}

      <button
        type="button"
        className="button button-secondary icon-button"
        onClick={onRetry}
        disabled={loading}
        aria-label="Reload inventory"
        title="Reload inventory"
      >
        <span aria-hidden="true">↻</span>
      </button>

      {pageControls}
    </div>
  );

  return (
    <section
      className={embedded ? "ledger-panel inventory-ledger" : "panel"}
      aria-label={embedded ? "Inventory" : undefined}
      aria-labelledby={embedded ? undefined : "inventory-title"}
      aria-busy={loading}
    >
      {embedded ? (
        <div className="ledger-toolbar">{tools}</div>
      ) : (
        <div className="panel-heading panel-heading-wrap">
          <div className="title-with-count">
            <h2 id="inventory-title">Inventory</h2>
            {!loading && !error && (
              <span>{items.length.toLocaleString("en-PH")}</span>
            )}
          </div>
          {tools}
        </div>
      )}

      {loading ? (
        <div className="loading-state panel-state" role="status" aria-live="polite">
          <span className="spinner" aria-hidden="true" />
          Loading…
        </div>
      ) : error ? (
        <div className="recovery-state" role="alert">
          <div>
            <strong>Inventory unavailable</strong>
            <span>{error}</span>
          </div>
          <button type="button" className="button button-secondary" onClick={onRetry}>
            Retry
          </button>
        </div>
      ) : items.length === 0 ? (
        <div className="empty-state">
          <strong>No items</strong>
        </div>
      ) : filtered.length === 0 ? (
        <div className="empty-state compact">
          <strong>No matches</strong>
        </div>
      ) : (
        <div className="table-region inventory-table-region">
          <div className="table-scroll inventory-table-scroll">
            <table>
              <caption className="sr-only">Inventory records</caption>
              <thead>
                <tr>
                  <th scope="col">Item</th>
                  <th scope="col">Location</th>
                  <th scope="col" className="align-right">
                    Stock
                  </th>
                </tr>
              </thead>

              <tbody>
                {pageItems.map((item) => {
                  const visiblePositions = [...(item.positions ?? [])]
                    .filter((position) => position.quantity > 0)
                    .sort((left, right) => {
                      if (left.location_code === UNASSIGNED) return 1;
                      if (right.location_code === UNASSIGNED) return -1;
                      return (
                        right.quantity - left.quantity ||
                        positionLabel(left).localeCompare(positionLabel(right))
                      );
                    });
                  const active = item.active !== false;

                  return (
                    <tr key={item.sku} className={active ? undefined : "archived-row"}>
                      <td>
                        <div className="inventory-identity">
                          <ProductThumbnail item={item} token={token} />
                          <span className="inventory-item-copy">
                            <strong className="inventory-item-name">
                              {item.product_name}
                            </strong>
                            {item.variant && (
                              <small className="item-variant">{item.variant}</small>
                            )}
                            <span className="inventory-item-references">
                              <code>{item.sku}</code>
                              <code>{item.barcode}</code>
                              {role === "admin" && (
                                <button
                                  type="button"
                                  className="row-action inventory-inline-edit"
                                  onClick={() => openEditor(item)}
                                  aria-label={`Edit ${item.product_name}`}
                                >
                                  Edit
                                </button>
                              )}
                            </span>
                          </span>
                        </div>
                      </td>

                      <td>
                        {visiblePositions.length ? (
                          <div className="inventory-positions">
                            {visiblePositions.map((position) => (
                              <span
                                key={position.location_code}
                                className={
                                  position.location_code === UNASSIGNED
                                    ? "inventory-position needs-placement"
                                    : "inventory-position"
                                }
                                title={positionLabel(position)}
                              >
                                <span className="inventory-position-name">
                                  <strong>
                                    {position.location_code === UNASSIGNED
                                      ? "Not recorded"
                                      : position.location_code}
                                  </strong>
                                  {positionShortName(position) && (
                                    <small>{positionShortName(position)}</small>
                                  )}
                                </span>
                                <b>
                                  {position.quantity.toLocaleString("en-PH")}
                                </b>
                              </span>
                            ))}
                          </div>
                        ) : (
                          <span className="cell-subtext">No stock</span>
                        )}
                      </td>

                      <td className="align-right inventory-stock-cell">
                        <span className="inventory-stock-total">
                          <strong>
                            {item.current_stock.toLocaleString("en-PH")}
                          </strong>
                          <small>{item.unit || "unit"}</small>
                          {role === "admin" &&
                            item.acquisition_cost_centavos != null && (
                              <small className="inventory-unit-cost">
                                {formatPhp(item.acquisition_cost_centavos)} ea.
                              </small>
                            )}
                        </span>
                        <span className="inventory-stock-state">
                          <span
                            className={`status ${
                              active
                                ? `status-${item.status.toLowerCase()}`
                                : "status-archived"
                            }`}
                          >
                            {active ? statusLabel(item.status) : "archived"}
                          </span>
                          <time
                            dateTime={item.last_updated}
                            title={formatTime(item.last_updated)}
                          >
                            {formatDate(item.last_updated)}
                          </time>
                        </span>
                      </td>

                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {editor && role === "admin" && (
        <div className="dialog-backdrop">
          <section
            className="product-editor"
            role="dialog"
            aria-modal="true"
            aria-labelledby="product-editor-title"
          >
            <header>
              <div>
                <span className="eyebrow">{editor.item.sku}</span>
                <h3 id="product-editor-title">Edit item</h3>
              </div>
              <button
                type="button"
                className="dialog-close"
                onClick={closeEditor}
                disabled={busy}
                aria-label="Close product editor"
              >
                ×
              </button>
            </header>

            <form onSubmit={saveProduct} className="product-editor-form" noValidate>
              <div className="product-media-editor">
                <ProductThumbnail item={editor.item} token={token} large />
                <div className="product-media-actions">
                  <input
                    ref={imageInputRef}
                    id="edit-product-image"
                    className="sr-only"
                    type="file"
                    accept="image/jpeg,image/png,image/webp"
                    onChange={uploadProductImage}
                    disabled={busy}
                  />
                  <button
                    type="button"
                    className="button button-secondary"
                    onClick={() => imageInputRef.current?.click()}
                    disabled={busy}
                  >
                    {imageAction === "upload"
                      ? "Uploading…"
                      : editor.item.image_url
                        ? "Replace photo"
                        : "Add photo"}
                  </button>
                  {editor.item.image_url && (
                    <button
                      type="button"
                      className={
                        imageRemoveArmed
                          ? "button button-danger"
                          : "button button-ghost"
                      }
                      onClick={removeProductImage}
                      disabled={busy}
                    >
                      {imageAction === "remove"
                        ? "Removing…"
                        : imageRemoveArmed
                          ? "Confirm remove"
                          : "Remove"}
                    </button>
                  )}
                </div>
                <span className="sr-only" role="status" aria-live="polite">
                  {imageStatus}
                </span>
              </div>

              <div className="form-grid">
                <div>
                  <label htmlFor="edit-name">Item</label>
                  <input
                    id="edit-name"
                    value={editor.productName}
                    onChange={(event) =>
                      updateEditor({ productName: event.target.value })
                    }
                    disabled={busy}
                    autoFocus
                  />
                </div>
                <div>
                  <label htmlFor="edit-variant">
                    Variant <span className="optional">optional</span>
                  </label>
                  <input
                    id="edit-variant"
                    value={editor.variant}
                    onChange={(event) =>
                      updateEditor({ variant: event.target.value })
                    }
                    disabled={busy}
                  />
                </div>
              </div>

              <div className="form-grid">
                <div>
                  <label htmlFor="edit-barcode">Barcode</label>
                  <input
                    id="edit-barcode"
                    value={editor.barcode}
                    onChange={(event) =>
                      updateEditor({ barcode: event.target.value })
                    }
                    disabled={busy}
                  />
                </div>
                <div>
                  <label htmlFor="edit-category">Category</label>
                  <input
                    id="edit-category"
                    value={editor.category}
                    onChange={(event) =>
                      updateEditor({ category: event.target.value })
                    }
                    disabled={busy}
                  />
                </div>
              </div>

              <div className="form-grid">
                <div>
                  <label htmlFor="edit-unit">Unit</label>
                  <input
                    id="edit-unit"
                    value={editor.unit}
                    onChange={(event) =>
                      updateEditor({ unit: event.target.value })
                    }
                    disabled={busy}
                  />
                </div>
                <div>
                  <label htmlFor="edit-acquisition-cost">
                    Acquisition cost <span className="optional">optional</span>
                  </label>
                  <div className="currency-input">
                    <span aria-hidden="true">₱</span>
                    <input
                      id="edit-acquisition-cost"
                      type="text"
                      inputMode="decimal"
                      autoComplete="off"
                      value={editor.acquisitionCostPhp}
                      onChange={(event) =>
                        updateEditor({ acquisitionCostPhp: event.target.value })
                      }
                      placeholder="0.00"
                      disabled={busy}
                    />
                  </div>
                </div>
              </div>

              {editorError && (
                <div className="alert alert-error" role="alert">
                  {editorError}
                </div>
              )}

              {archiveArmed && editor.item.active !== false && (
                <div className="archive-confirmation" role="status">
                  <strong>Archive this item?</strong>
                  <span>
                    History and{" "}
                    {editor.item.current_stock.toLocaleString("en-PH")} recorded{" "}
                    {editor.item.unit || "unit"}
                    {editor.item.current_stock === 1 ? "" : "s"} remain visible.
                  </span>
                </div>
              )}

              <footer>
                <button
                  type="button"
                  className={
                    archiveArmed ? "button button-danger" : "button button-ghost"
                  }
                  onClick={changeArchiveState}
                  disabled={busy}
                >
                  {editor.item.active === false
                    ? "Restore"
                    : archiveArmed
                      ? "Confirm archive"
                      : "Archive"}
                </button>

                <div className="button-row">
                  <button
                    type="button"
                    className="button button-secondary"
                    onClick={closeEditor}
                    disabled={busy}
                  >
                    Cancel
                  </button>
                  <button type="submit" className="button button-primary" disabled={busy}>
                    {saving ? "Saving…" : "Save"}
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
