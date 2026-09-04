"use client";

import {
  ChangeEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ApiError,
  stockpileApi,
} from "../lib/api";
import type {
  AIDiscrepancyFinding,
  AIReviewRecord,
  DeliveryDocumentAIReview,
  DeliveryDocumentLineMatch,
  DeliveryDocumentMatchStatus,
  DiscrepancyAIReview,
  Role,
} from "../lib/types";

const MAX_DOCUMENT_BYTES = 5_000_000;

type ReviewMode =
  | "document"
  | "daily";

type Notice = {
  tone: "success" | "error";
  text: string;
};

interface DocumentAttempt {
  file: File;
  key: string;
}

export interface AIDeliveryDraftLine {
  barcode: string;
  product_name: string;
  quantity: number;
  unit: string;
}

export interface AIDeliveryDraft {
  source_review_id: string;
  supplier_name: string | null;
  reference: string | null;
  document_date: string | null;
  lines: AIDeliveryDraftLine[];
}

interface AIReviewPanelProps {
  token: string;
  role: Role;
  onUnauthorized?: () => void;
  onPrepareDelivery?: (
    draft: AIDeliveryDraft,
  ) => void;
  embedded?: boolean;
}

function newReviewKey(prefix: string) {
  if (globalThis.crypto?.randomUUID) {
    return `${prefix}-${globalThis.crypto.randomUUID()}`;
  }

  return `${prefix}-${Date.now()}-${Math.random()
    .toString(36)
    .slice(2)}`.slice(0, 128);
}

function fileSize(value: number) {
  if (value < 1_000_000) {
    return `${Math.max(1, Math.round(value / 1_000))} KB`;
  }
  return `${(value / 1_000_000).toFixed(1)} MB`;
}

function readableError(
  error: unknown,
  fallback: string,
) {
  return error instanceof Error
    ? error.message
    : fallback;
}

function matchLabel(
  status: DeliveryDocumentMatchStatus,
) {
  switch (status) {
    case "matched":
      return "Matched";
    case "unknown_product":
      return "Unknown";
    case "identifier_conflict":
      return "Conflict";
    case "inactive_product":
      return "Inactive";
    case "invalid_quantity":
      return "Check quantity";
    case "duplicate_product":
      return "Repeated";
  }

  return "Check";
}

function findingTone(
  finding: AIDiscrepancyFinding,
) {
  if (finding.priority === "high") return "high";
  if (finding.priority === "medium") return "medium";
  return "low";
}

function DocumentResult({
  review,
  onPrepareDelivery,
}: {
  review: DeliveryDocumentAIReview;
  onPrepareDelivery?: (
    draft: AIDeliveryDraft,
  ) => void;
}) {
  const analysis = review.result;
  if (review.status === "processing") {
    return (
      <div className="loading-state" role="status">
        <span className="spinner" aria-hidden="true" />
        Reading…
      </div>
    );
  }
  if (review.status === "failed") {
    return (
      <div className="ai-result-failure" role="alert">
        <strong>Review failed</strong>
        <span>{review.error?.message || "Try the document again."}</span>
      </div>
    );
  }
  if (!analysis) {
    return <div className="empty-state"><strong>No result</strong></div>;
  }

  const matchByLine = new Map<number, DeliveryDocumentLineMatch>(
    analysis.matches.map((match) => [match.source_line, match]),
  );
  const draftLines = analysis.extraction.lines.flatMap((line) => {
    const match = matchByLine.get(line.source_line);
    if (
      match?.status !== "matched" ||
      !match.product ||
      line.quantity === null
    ) {
      return [];
    }
    return [
      {
        barcode: match.product.barcode,
        product_name: match.product.product_name,
        quantity: line.quantity,
        unit: match.product.unit,
      },
    ];
  });

  function prepareDelivery() {
    if (!onPrepareDelivery || !analysis) return;
    onPrepareDelivery({
      source_review_id: review.id,
      supplier_name: analysis.extraction.supplier_name,
      reference: analysis.extraction.reference,
      document_date: analysis.extraction.document_date,
      lines: draftLines,
    });
  }

  return (
    <div className="ai-document-result">
      <header className="ai-result-head">
        <div>
          <span className="eyebrow">
            {analysis.extraction.document_type === "delivery_receipt"
              ? "Delivery receipt"
              : analysis.extraction.document_type === "purchase_order"
                ? "Purchase order"
                : analysis.extraction.document_type === "invoice"
                  ? "Invoice"
                  : "Document"}
          </span>
          <h3>
            {analysis.extraction.supplier_name ||
              review.source.filename ||
              "Delivery document"}
          </h3>
          <p className="ai-result-reference">
            {[
              analysis.extraction.reference,
              analysis.extraction.document_date,
            ]
              .filter(Boolean)
              .join(" · ") || "No reference found"}
          </p>
        </div>
        <span
          className={`status-pill ${
            analysis.receipt_ready
              ? "status-success"
              : "status-warning"
          }`}
        >
          {analysis.receipt_ready ? "Ready" : "Check"}
        </span>
      </header>

      {analysis.extraction.warnings.length > 0 && (
        <div className="ai-warning-strip">
          {analysis.extraction.warnings.join(" · ")}
        </div>
      )}

      <div className="ai-lines-scroll" tabIndex={0}>
        <table className="ai-lines-table">
          <thead>
            <tr>
              <th>Line</th>
              <th>Item</th>
              <th className="numeric">Qty</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {analysis.extraction.lines.map((line) => {
              const match = matchByLine.get(line.source_line);
              return (
                <tr key={line.source_line}>
                  <td className="ai-source-line">
                    {line.source_line.toString().padStart(2, "0")}
                  </td>
                  <td>
                    <strong>
                      {match?.product?.product_name ||
                        line.description ||
                        line.sku ||
                        line.barcode ||
                        "Unreadable item"}
                    </strong>
                    <span>
                      {match?.product?.sku ||
                        line.sku ||
                        line.barcode ||
                        line.source_text}
                    </span>
                  </td>
                  <td className="numeric">
                    {line.quantity === null
                      ? "—"
                      : line.quantity.toLocaleString("en-PH")}
                    {line.unit ? ` ${line.unit}` : ""}
                  </td>
                  <td>
                    <span
                      className={`status-pill ${
                        match?.status === "matched"
                          ? "status-success"
                          : "status-warning"
                      }`}
                    >
                      {match ? matchLabel(match.status) : "Check"}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {analysis.receipt_ready && onPrepareDelivery && (
        <div className="ai-result-actions">
          <button
            type="button"
            className="button button-primary"
            onClick={prepareDelivery}
          >
            Prepare delivery
          </button>
        </div>
      )}
    </div>
  );
}

function DiscrepancyResult({
  review,
}: {
  review: DiscrepancyAIReview;
}) {
  if (review.status === "processing") {
    return (
      <div className="loading-state" role="status">
        <span className="spinner" aria-hidden="true" />
        Reviewing…
      </div>
    );
  }
  if (review.status === "failed") {
    return (
      <div className="ai-result-failure" role="alert">
        <strong>Review failed</strong>
        <span>{review.error?.message || "Run the review again."}</span>
      </div>
    );
  }
  if (!review.result) {
    return <div className="empty-state"><strong>No result</strong></div>;
  }

  const result = review.result;
  return (
    <div className="ai-discrepancy-result">
      <header className="ai-result-head">
        <h3>
          {result.review_status === "no_issues"
            ? "All clear"
            : result.review_status === "insufficient_evidence"
              ? "More evidence needed"
              : `${result.findings.length} to check`}
        </h3>
      </header>

      {result.findings.length === 0 && (
        <p className="ai-review-summary">{result.summary}</p>
      )}

      {result.findings.length > 0 && (
        <div className="ai-findings">
          {result.findings.map((finding, index) => (
            <article
              className={`ai-finding priority-${findingTone(finding)}`}
              key={`${finding.title}-${index}`}
            >
              <span className="ai-finding-index">
                {(index + 1).toString().padStart(2, "0")}
              </span>
              <div>
                <header>
                  <h4>{finding.title}</h4>
                  <span>{finding.priority}</span>
                </header>
                <p>{finding.explanation}</p>
                <strong className="ai-next-step">
                  {finding.next_step}
                </strong>
              </div>
            </article>
          ))}
        </div>
      )}

    </div>
  );
}

function ReviewProgress({ mode }: { mode: ReviewMode }) {
  return (
    <div className="ai-run-state" role="status" aria-live="polite">
      <span className="ai-run-track" aria-hidden="true" />
      <strong>
        {mode === "document" ? "Reading document" : "Checking records"}
      </strong>
    </div>
  );
}

function DocumentPreview({
  filename,
  source,
}: {
  filename: string;
  source: string;
}) {
  return (
    <div className="ai-document-preview">
      <img src={source} alt={`${filename} preview`} />
    </div>
  );
}

function ReviewResult({
  review,
  mode,
  onPrepareDelivery,
}: {
  review: AIReviewRecord | null;
  mode: ReviewMode;
  onPrepareDelivery?: (
    draft: AIDeliveryDraft,
  ) => void;
}) {
  if (!review) {
    return (
      <div className="ai-result-empty">
        <strong>
          {mode === "document" ? "Ready for a document" : "Ready to check"}
        </strong>
      </div>
    );
  }

  return review.review_type === "delivery_document" ? (
    <DocumentResult
      review={review}
      onPrepareDelivery={onPrepareDelivery}
    />
  ) : (
    <DiscrepancyResult review={review} />
  );
}

export default function AIReviewPanel({
  token,
  role,
  onUnauthorized,
  onPrepareDelivery,
  embedded = false,
}: AIReviewPanelProps) {
  const [mode, setMode] = useState<ReviewMode>("document");
  const [reviews, setReviews] = useState<AIReviewRecord[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [document, setDocument] = useState<File | null>(null);
  const [documentPreview, setDocumentPreview] = useState<string | null>(null);
  const [loading, setLoading] = useState(role === "admin");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [documentRunning, setDocumentRunning] = useState(false);
  const [dailyRunning, setDailyRunning] = useState(false);
  const [sampleLoading, setSampleLoading] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const documentAttempt = useRef<DocumentAttempt | null>(null);
  const dailyAttempt = useRef<string | null>(null);
  const accountScope = `${role}\u001f${token}`;
  const accountScopeRef = useRef(accountScope);
  const [stateScope, setStateScope] = useState(accountScope);

  if (accountScopeRef.current !== accountScope) {
    accountScopeRef.current = accountScope;
  }

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

  useEffect(() => {
    accountScopeRef.current = accountScope;
    setReviews([]);
    setSelectedId(null);
    setDocument(null);
    setLoadError(null);
    setNotice(null);
    setDocumentRunning(false);
    setDailyRunning(false);
    setSampleLoading(false);
    documentAttempt.current = null;
    dailyAttempt.current = null;
    setLoading(role === "admin");
    setStateScope(accountScope);
  }, [accountScope, role]);

  useEffect(() => {
    if (!document) {
      setDocumentPreview(null);
      return;
    }

    const preview = URL.createObjectURL(document);
    setDocumentPreview(preview);
    return () => URL.revokeObjectURL(preview);
  }, [document]);

  const loadReviews = useCallback(async () => {
    if (role !== "admin") {
      setLoading(false);
      return;
    }

    setLoading(true);
    setLoadError(null);
    const requestedScope = accountScope;
    try {
      const next = await stockpileApi.aiReviews(token, undefined, 30);
      if (accountScopeRef.current !== requestedScope) return;
      setReviews(next);
      setSelectedId((current) =>
        current && next.some((review) => review.id === current)
          ? current
          : null,
      );
    } catch (caught) {
      if (accountScopeRef.current !== requestedScope) return;
      setLoadError(
        handleError(caught, "Reviews could not be loaded."),
      );
    } finally {
      if (accountScopeRef.current === requestedScope) {
        setLoading(false);
      }
    }
  }, [accountScope, handleError, role, token]);

  useEffect(() => {
    void loadReviews();
  }, [loadReviews]);

  const modeReviews = useMemo(
    () =>
      reviews.filter((review) =>
        mode === "document"
          ? review.review_type === "delivery_document"
          : review.review_type === "discrepancy_review",
      ),
    [mode, reviews],
  );

  const selectedReview = useMemo(
    () =>
      reviews.find((review) => review.id === selectedId) ||
      modeReviews[0] ||
      null,
    [modeReviews, reviews, selectedId],
  );

  useEffect(() => {
    if (
      selectedReview &&
      ((mode === "document" &&
        selectedReview.review_type !== "delivery_document") ||
        (mode === "daily" &&
          selectedReview.review_type !== "discrepancy_review"))
    ) {
      setSelectedId(modeReviews[0]?.id || null);
    }
  }, [mode, modeReviews, selectedReview]);

  function selectDocument(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0] || null;
    event.currentTarget.value = "";
    if (!file) return;

    if (file.size === 0) {
      setNotice({ tone: "error", text: "Choose a document image." });
      return;
    }
    if (file.size > MAX_DOCUMENT_BYTES) {
      setNotice({ tone: "error", text: "Image must be 5 MB or smaller." });
      return;
    }

    documentAttempt.current = {
      file,
      key: newReviewKey("document-review"),
    };
    setDocument(file);
    setNotice(null);
  }

  async function useSampleDocument() {
    setSampleLoading(true);
    setNotice(null);
    try {
      const response = await fetch("/sample-delivery-receipt.png", {
        cache: "no-store",
      });
      if (!response.ok) {
        throw new Error("The sample receipt could not be loaded.");
      }
      const blob = await response.blob();
      const file = new File([blob], "sample-delivery-receipt.png", {
        type: blob.type || "image/png",
      });
      documentAttempt.current = {
        file,
        key: newReviewKey("document-review"),
      };
      setDocument(file);
    } catch (caught) {
      setNotice({
        tone: "error",
        text: readableError(caught, "The sample receipt could not be loaded."),
      });
    } finally {
      setSampleLoading(false);
    }
  }

  async function runDocumentReview() {
    if (documentRunning || dailyRunning) return;

    if (!document) {
      fileInput.current?.click();
      return;
    }

    if (documentAttempt.current?.file !== document) {
      documentAttempt.current = {
        file: document,
        key: newReviewKey("document-review"),
      };
    }

    const attempt = documentAttempt.current;
    const requestedScope = accountScope;
    setDocumentRunning(true);
    setNotice(null);
    try {
      const response = await stockpileApi.reviewDeliveryDocument(
        token,
        attempt.key,
        document,
      );
      if (accountScopeRef.current !== requestedScope) return;
      documentAttempt.current = null;
      setReviews((current) => [
        response.data,
        ...current.filter((review) => review.id !== response.data.id),
      ]);
      setSelectedId(response.data.id);
      setNotice(null);
    } catch (caught) {
      if (accountScopeRef.current !== requestedScope) return;
      setNotice({
        tone: "error",
        text: handleError(caught, "Document review failed."),
      });
      await loadReviews();
    } finally {
      if (accountScopeRef.current === requestedScope) {
        setDocumentRunning(false);
      }
    }
  }

  async function runDailyReview() {
    if (documentRunning || dailyRunning) return;

    if (!dailyAttempt.current) {
      dailyAttempt.current = newReviewKey("daily-review");
    }

    const requestedScope = accountScope;
    setDailyRunning(true);
    setNotice(null);
    try {
      const response = await stockpileApi.reviewDiscrepancies(
        token,
        dailyAttempt.current,
      );
      if (accountScopeRef.current !== requestedScope) return;
      dailyAttempt.current = null;
      setReviews((current) => [
        response.data,
        ...current.filter((review) => review.id !== response.data.id),
      ]);
      setSelectedId(response.data.id);
      setNotice(null);
    } catch (caught) {
      if (accountScopeRef.current !== requestedScope) return;
      setNotice({
        tone: "error",
        text: handleError(caught, "Review failed."),
      });
      await loadReviews();
    } finally {
      if (accountScopeRef.current === requestedScope) {
        setDailyRunning(false);
      }
    }
  }

  if (role !== "admin") {
    return (
      <section
        className={`ai-review-panel${embedded ? " embedded-panel" : ""}`}
        aria-label="Review"
      >
        <div className="empty-state">
          <strong>Owner access required</strong>
        </div>
      </section>
    );
  }

  if (stateScope !== accountScope) {
    return (
      <section
        className={`ai-review-panel${embedded ? " embedded-panel" : ""}`}
        aria-label="Review"
      >
        <div className="loading-state" role="status">
          <span className="spinner" aria-hidden="true" /> Loading…
        </div>
      </section>
    );
  }

  return (
    <section
      className={`ai-review-panel${embedded ? " embedded-panel" : ""}`}
      aria-label="Review"
    >
      <header className="ai-review-head">
        <div className="ai-review-tabs" role="tablist" aria-label="Review type">
          <button
            type="button"
            role="tab"
            id="ai-document-tab"
            aria-selected={mode === "document"}
            aria-controls="ai-review-workspace"
            className={mode === "document" ? "active" : ""}
            disabled={documentRunning || dailyRunning}
            onClick={() => {
              setMode("document");
              setSelectedId(null);
              setNotice(null);
            }}
          >
            Document
          </button>
          <button
            type="button"
            role="tab"
            id="ai-daily-tab"
            aria-selected={mode === "daily"}
            aria-controls="ai-review-workspace"
            className={mode === "daily" ? "active" : ""}
            disabled={documentRunning || dailyRunning}
            onClick={() => {
              setMode("daily");
              setSelectedId(null);
              setNotice(null);
            }}
          >
            Open issues
          </button>
        </div>
      </header>

      {notice && (
        <div
          className={`alert ${
            notice.tone === "error" ? "alert-error" : "alert-success"
          }`}
          role={notice.tone === "error" ? "alert" : "status"}
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

      {loadError && reviews.length > 0 && (
        <div className="alert alert-error" role="alert">
          <span>{loadError}</span>
          <button type="button" className="text-button" onClick={loadReviews}>
            Retry
          </button>
        </div>
      )}

      <div
        id="ai-review-workspace"
        className="ai-review-workspace"
        role="tabpanel"
        aria-labelledby={mode === "document" ? "ai-document-tab" : "ai-daily-tab"}
      >
        <div
          className={`ai-review-command${
            mode === "daily" ? " ai-review-command-single" : ""
          }`}
        >
          {mode === "document" ? (
            <>
              <input
                ref={fileInput}
                type="file"
                accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp"
                onChange={selectDocument}
                hidden
              />
              <button
                type="button"
                className="ai-file-select"
                onClick={() => fileInput.current?.click()}
                disabled={documentRunning || sampleLoading}
              >
                <span aria-hidden="true">DOC</span>
                <strong>{document?.name || "Choose document"}</strong>
                <small>
                  {document ? fileSize(document.size) : "JPEG · PNG · WebP"}
                </small>
              </button>
              <button
                type="button"
                className="button button-secondary ai-sample-button"
                onClick={useSampleDocument}
                disabled={documentRunning || sampleLoading}
              >
                {sampleLoading ? "Loading…" : "Sample receipt"}
              </button>
              <button
                type="button"
                className="button button-primary"
                onClick={() => void runDocumentReview()}
                disabled={documentRunning || dailyRunning}
              >
                {documentRunning ? "Reading…" : "Review document"}
              </button>
            </>
          ) : (
            <button
              type="button"
              className="button button-primary"
              onClick={() => void runDailyReview()}
              disabled={documentRunning || dailyRunning}
            >
              {dailyRunning ? "Checking…" : "Check records"}
            </button>
          )}
        </div>

        <div className="ai-review-layout ai-review-layout-single">
          <div
            className="ai-review-output"
            aria-busy={documentRunning || dailyRunning}
          >
            {documentRunning || dailyRunning ? (
              <ReviewProgress mode={documentRunning ? "document" : "daily"} />
            ) : mode === "document" &&
              document &&
              documentPreview &&
              documentAttempt.current?.file === document ? (
              <DocumentPreview
                filename={document.name}
                source={documentPreview}
              />
            ) : loading && reviews.length === 0 ? (
              <div className="loading-state" role="status">
                <span className="spinner" aria-hidden="true" /> Loading…
              </div>
            ) : loadError && reviews.length === 0 ? (
              <div className="empty-state" role="alert">
                <strong>Reviews unavailable</strong>
                <button
                  type="button"
                  className="button button-secondary"
                  onClick={loadReviews}
                >
                  Retry
                </button>
              </div>
            ) : (
              <ReviewResult
                review={selectedReview}
                mode={mode}
                onPrepareDelivery={onPrepareDelivery}
              />
            )}

          </div>
        </div>
      </div>
    </section>
  );
}
