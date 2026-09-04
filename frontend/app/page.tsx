"use client";

import Image from "next/image";
import {
  ChangeEvent,
  KeyboardEvent as ReactKeyboardEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import AIReviewPanel from "../components/AIReviewPanel";
import HistoryPanel from "../components/HistoryPanel";
import InventoryTable from "../components/InventoryTable";
import LoginScreen from "../components/LoginScreen";
import OrderComparisonPanel from "../components/OrderComparisonPanel";
import ScannerTransaction from "../components/ScannerTransaction";
import StockControlPanel from "../components/StockControlPanel";
import { ApiError, stockpileApi } from "../lib/api";
import type {
  InventoryItem,
  InventoryTransaction,
  StockLocation,
  User,
} from "../lib/types";

const SESSION_KEY = "stockpile_session_token";
const MAX_IMPORT_BYTES = 2_000_000;
const LOGIN_EXIT_MS = 360;
const APP_ENTRY_MS = 450;
const ARRIVAL_NOTICE_MS = 1500;

type LedgerView =
  | "inventory"
  | "actions"
  | "orders"
  | "review"
  | "history";

type Notice = { tone: "success" | "error"; text: string };

const OPERATOR_LEDGER_VIEWS: LedgerView[] = [
  "inventory",
  "actions",
  "orders",
  "history",
];

const OWNER_LEDGER_VIEWS: LedgerView[] = [
  "inventory",
  "actions",
  "orders",
  "review",
  "history",
];

const LEDGER_LABELS: Record<LedgerView, string> = {
  inventory: "Inventory",
  actions: "Stock control",
  orders: "Order check",
  review: "AI review",
  history: "History",
};

function readableError(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

async function importKey(file: File) {
  if (globalThis.crypto?.subtle) {
    const digest = await globalThis.crypto.subtle.digest(
      "SHA-256",
      await file.arrayBuffer(),
    );
    const hash = Array.from(new Uint8Array(digest), (byte) =>
      byte.toString(16).padStart(2, "0"),
    ).join("");
    return `csv-${hash}`;
  }

  return `csv-${file.name}-${file.size}-${file.lastModified}`.slice(0, 128);
}

export default function Home() {
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [restoring, setRestoring] = useState(true);
  const [entering, setEntering] = useState(false);
  const [showArrival, setShowArrival] = useState(false);

  const [inventory, setInventory] = useState<InventoryItem[]>([]);
  const [locations, setLocations] = useState<StockLocation[]>([]);
  const [transactions, setTransactions] = useState<InventoryTransaction[]>([]);

  const [inventoryLoading, setInventoryLoading] = useState(true);
  const [locationsLoading, setLocationsLoading] = useState(true);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [importing, setImporting] = useState(false);

  const [inventoryError, setInventoryError] = useState<string | null>(null);
  const [locationsError, setLocationsError] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);

  const [notice, setNotice] = useState<Notice | null>(null);
  const [activeView, setActiveView] = useState<LedgerView>("inventory");
  const importInput = useRef<HTMLInputElement>(null);

  const signOut = useCallback(() => {
    window.sessionStorage.removeItem(SESSION_KEY);
    setToken(null);
    setUser(null);
    setInventory([]);
    setLocations([]);
    setTransactions([]);
    setInventoryLoading(true);
    setLocationsLoading(true);
    setHistoryLoading(true);
    setInventoryError(null);
    setLocationsError(null);
    setHistoryError(null);
    setNotice(null);
    setActiveView("inventory");
    setEntering(false);
    setShowArrival(false);
  }, []);

  const handleResourceError = useCallback(
    (error: unknown, fallback: string) => {
      if (error instanceof ApiError && error.status === 401) {
        signOut();
        return "Your session expired.";
      }

      return readableError(error, fallback);
    },
    [signOut],
  );

  useEffect(() => {
    const savedToken = window.sessionStorage.getItem(SESSION_KEY);

    if (!savedToken) {
      setRestoring(false);
      return;
    }

    stockpileApi
      .me(savedToken)
      .then((response) => {
        setToken(savedToken);
        setUser(response.user);
      })
      .catch(() => window.sessionStorage.removeItem(SESSION_KEY))
      .finally(() => setRestoring(false));
  }, []);

  const loadInventory = useCallback(async () => {
    if (!token) return;

    setInventoryLoading(true);
    setInventoryError(null);

    try {
      setInventory(await stockpileApi.inventory(token));
    } catch (error) {
      setInventoryError(
        handleResourceError(error, "Inventory could not be loaded."),
      );
    } finally {
      setInventoryLoading(false);
    }
  }, [handleResourceError, token]);

  const loadLocations = useCallback(async () => {
    if (!token) return;

    setLocationsLoading(true);
    setLocationsError(null);

    try {
      setLocations(await stockpileApi.locations(token));
    } catch (error) {
      setLocationsError(
        handleResourceError(error, "Locations could not be loaded."),
      );
    } finally {
      setLocationsLoading(false);
    }
  }, [handleResourceError, token]);

  const loadHistory = useCallback(async () => {
    if (!token) return;

    setHistoryLoading(true);
    setHistoryError(null);

    try {
      setTransactions(await stockpileApi.transactions(token));
    } catch (error) {
      setHistoryError(
        handleResourceError(error, "History could not be loaded."),
      );
    } finally {
      setHistoryLoading(false);
    }
  }, [handleResourceError, token]);

  const loadAll = useCallback(async () => {
    await Promise.all([loadInventory(), loadLocations(), loadHistory()]);
  }, [loadHistory, loadInventory, loadLocations]);

  const loadMovementResults = useCallback(async () => {
    await Promise.all([loadInventory(), loadHistory()]);
  }, [loadHistory, loadInventory]);

  useEffect(() => {
    if (token && user) {
      void loadAll();
    }
  }, [loadAll, token, user]);

  async function login(username: string, password: string) {
    const response = await stockpileApi.login(username, password);

    const reduceMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;

    setEntering(true);
    await new Promise((resolve) =>
      window.setTimeout(resolve, reduceMotion ? 0 : LOGIN_EXIT_MS),
    );

    setInventoryLoading(true);
    setLocationsLoading(true);
    setHistoryLoading(true);
    setInventoryError(null);
    setLocationsError(null);
    setHistoryError(null);
    setNotice(null);
    setActiveView("inventory");

    window.sessionStorage.setItem(SESSION_KEY, response.token);
    setToken(response.token);
    setUser(response.user);
    setShowArrival(true);
    window.setTimeout(
      () => setEntering(false),
      reduceMotion ? 0 : APP_ENTRY_MS,
    );
    window.setTimeout(
      () => setShowArrival(false),
      reduceMotion ? 900 : ARRIVAL_NOTICE_MS,
    );
  }

  async function handleTransactionComplete() {
    setNotice(null);
    await loadMovementResults();
  }

  async function handleReverse(id: string, reason: string, key: string) {
    if (!token) {
      throw new Error("Your session has ended.");
    }

    const transaction = transactions.find((entry) => entry.id === id);

    if (
      transaction &&
      !["scanner", "manual", "import"].includes(transaction.source)
    ) {
      throw new Error("This record is managed in Stock control.");
    }

    try {
      await stockpileApi.reverseTransaction(token, id, key, reason);
      await loadMovementResults();
      setNotice({ tone: "success", text: "Reversal recorded." });
    } catch (error) {
      throw new Error(handleResourceError(error, "Reversal failed."));
    }
  }

  async function handleImport(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";

    if (!file || !token) return;

    if (file.size > MAX_IMPORT_BYTES) {
      setNotice({ tone: "error", text: "CSV must be smaller than 2 MB." });
      return;
    }

    setImporting(true);
    setNotice(null);

    try {
      const result = await stockpileApi.importInventory(
        token,
        await importKey(file),
        file,
      );

      await loadMovementResults();

      const summary = [
        `${result.created.toLocaleString()} added`,
        `${result.updated.toLocaleString()} updated`,
        `${result.skipped.toLocaleString()} unchanged`,
      ].join(" · ");

      setNotice({
        tone: result.errors.length ? "error" : "success",
        text: result.errors.length
          ? `${summary} · ${result.errors[0]}${
              result.errors.length > 1
                ? ` (+${(result.errors.length - 1).toLocaleString()} more)`
                : ""
            }`
          : summary,
      });
    } catch (error) {
      setNotice({
        tone: "error",
        text: handleResourceError(error, "Import failed."),
      });
    } finally {
      setImporting(false);
    }
  }

  function moveLedgerTab(event: ReactKeyboardEvent<HTMLButtonElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      return;
    }

    event.preventDefault();

    const views =
      user?.role === "admin"
        ? OWNER_LEDGER_VIEWS
        : OPERATOR_LEDGER_VIEWS;

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
    document.getElementById(`${next}-tab`)?.focus();
  }

  if (!user || !token) {
    return (
      <LoginScreen
        onLogin={login}
        restoring={restoring}
        entering={entering}
      />
    );
  }

  const initials = user.display_name
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
  const firstName = user.display_name.trim().split(/\s+/)[0];

  const visibleViews =
    user.role === "admin" ? OWNER_LEDGER_VIEWS : OPERATOR_LEDGER_VIEWS;

  return (
    <div className={`app-shell${entering ? " app-shell-entering" : ""}`}>
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>

      {showArrival && (
        <div className="session-arrival" role="status" aria-live="polite">
          Welcome back{firstName ? `, ${firstName}` : ""}.
        </div>
      )}

      <header className="app-header">
        <div className="brand-lockup">
          <span className="logo-tile">
            <Image
              src="/app-logo.png"
              width={34}
              height={34}
              alt="Hermit Edge"
              priority
            />
          </span>

          <div className="brand-name">
            <h1>Stockpile</h1>
            <small>AI Inventory System</small>
          </div>
        </div>

        <div className="header-context">
          <div className="user-chip">
            <span className="avatar" aria-hidden="true">
              {initials || "U"}
            </span>

            <span>
              <strong>{user.display_name}</strong>
              <small>{user.role === "admin" ? "Owner" : "Staff"}</small>
            </span>
          </div>

          <button
            type="button"
            className="button button-ghost"
            onClick={signOut}
          >
            Sign out
          </button>
        </div>
      </header>

      <main id="main-content" className="app-content">
        {notice && (
          <div
            className={`global-notice notice-${notice.tone}`}
            role={notice.tone === "error" ? "alert" : "status"}
            aria-live="polite"
          >
            <span>{notice.text}</span>

            <button
              type="button"
              className="text-button"
              onClick={() => setNotice(null)}
            >
              Dismiss
            </button>
          </div>
        )}

        <div className="operations-layout">
          <ScannerTransaction
            token={token}
            role={user.role}
            locations={locations}
            locationsLoading={locationsLoading}
            locationsError={locationsError}
            onRetryLocations={loadLocations}
            onComplete={handleTransactionComplete}
          />

          <section className="ledger-shell" aria-label="Stock workspace">
            <div className="ledger-head">
              <div
                className="ledger-tabs"
                role="tablist"
                aria-label="Stockpile views"
              >
                {visibleViews.map((view) => {
                  const count =
                    view === "inventory" &&
                    !inventoryLoading &&
                    !inventoryError
                      ? inventory.length
                      : view === "history" &&
                          !historyLoading &&
                          !historyError
                        ? transactions.length
                        : null;

                  return (
                    <button
                      key={view}
                      id={`${view}-tab`}
                      type="button"
                      role="tab"
                      aria-selected={activeView === view}
                      aria-controls="ledger-panel"
                      tabIndex={activeView === view ? 0 : -1}
                      className={activeView === view ? "active" : ""}
                      onClick={() => setActiveView(view)}
                      onKeyDown={moveLedgerTab}
                    >
                      {LEDGER_LABELS[view]}
                      {count !== null && (
                        <span>{count.toLocaleString("en-PH")}</span>
                      )}
                    </button>
                  );
                })}
              </div>

              {user.role === "admin" && activeView === "inventory" && (
                <>
                  <input
                    ref={importInput}
                    type="file"
                    accept=".csv,text/csv"
                    onChange={handleImport}
                    hidden
                  />

                  <button
                    type="button"
                    className="button button-secondary import-button"
                    onClick={() => importInput.current?.click()}
                    disabled={importing}
                  >
                    {importing ? "Importing…" : "Import CSV"}
                  </button>
                </>
              )}
            </div>

            <div
              id="ledger-panel"
              className="ledger-stage"
              role="tabpanel"
              aria-labelledby={`${activeView}-tab`}
            >
              {activeView === "inventory" && (
                <InventoryTable
                  items={inventory}
                  locations={locations}
                  token={token}
                  role={user.role}
                  loading={inventoryLoading}
                  error={inventoryError}
                  onRetry={loadInventory}
                  onChanged={loadMovementResults}
                  embedded
                />
              )}

              {activeView === "actions" && (
                <StockControlPanel
                  token={token}
                  role={user.role}
                  items={inventory}
                  locations={locations}
                  onChanged={loadMovementResults}
                  embedded
                />
              )}

              {activeView === "orders" && (
                <OrderComparisonPanel
                  token={token}
                  role={user.role}
                  transactions={transactions}
                  onUnauthorized={signOut}
                  embedded
                />
              )}

              {activeView === "review" && user.role === "admin" && (
                <AIReviewPanel
                  token={token}
                  role={user.role}
                  onUnauthorized={signOut}
                  embedded
                />
              )}

              {activeView === "history" && (
                <HistoryPanel
                  transactions={transactions}
                  role={user.role}
                  loading={historyLoading}
                  error={historyError}
                  onRetry={loadHistory}
                  onReverse={handleReverse}
                  embedded
                />
              )}
            </div>
          </section>
        </div>
      </main>

      <footer className="site-footer app-footer">
        © 2026 Emman Ermitaño. All rights reserved.
      </footer>
    </div>
  );
}
