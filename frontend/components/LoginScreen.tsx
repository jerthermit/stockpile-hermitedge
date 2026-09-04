"use client";

import Image from "next/image";
import { FormEvent, useState } from "react";

interface LoginScreenProps {
  onLogin: (username: string, password: string) => Promise<void>;
  restoring: boolean;
  entering: boolean;
}

export default function LoginScreen({
  onLogin,
  restoring,
  entering,
}: LoginScreenProps) {
  const legacyDemoUsername =
    process.env.NEXT_PUBLIC_DEMO_USERNAME?.trim() ?? "";
  const legacyDemoPassword =
    process.env.NEXT_PUBLIC_DEMO_PASSWORD ?? "";
  const demoAccounts = [
    {
      label: "Owner",
      username:
        process.env.NEXT_PUBLIC_DEMO_OWNER_USERNAME?.trim() ||
        legacyDemoUsername,
      password:
        process.env.NEXT_PUBLIC_DEMO_OWNER_PASSWORD ||
        legacyDemoPassword,
    },
    {
      label: "Staff",
      username:
        process.env.NEXT_PUBLIC_DEMO_STAFF_USERNAME?.trim() ?? "",
      password:
        process.env.NEXT_PUBLIC_DEMO_STAFF_PASSWORD ?? "",
    },
  ].filter((account) => account.username && account.password);

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selectedAccount = demoAccounts.find(
    (account) =>
      username === account.username && password === account.password,
  );

  async function handleSubmit(
    event: FormEvent<HTMLFormElement>,
  ) {
    event.preventDefault();

    if (!username.trim() || !password) {
      setError("Enter your username and password.");
      return;
    }

    setSubmitting(true);
    setError(null);

    try {
      await onLogin(username.trim(), password);
    } catch (loginError) {
      setError(
        loginError instanceof Error
          ? loginError.message
          : "Sign in failed.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main
      className={`login-shell${entering ? " login-shell-entering" : ""}`}
    >
      <header className="login-brand">
        <span className="logo-tile logo-tile-large">
          <Image
            src="/app-logo.png"
            width={44}
            height={44}
            alt="Hermit Edge"
            priority
          />
        </span>

        <div className="brand-name login-brand-name">
          <h1 id="login-title">Stockpile</h1>
          <small>AI Inventory System</small>
        </div>
      </header>

      <section
        className="login-access"
        aria-labelledby="login-title"
      >
        {restoring ? (
          <div
            className="loading-state login-loading"
            role="status"
            aria-live="polite"
          >
            <span className="spinner" aria-hidden="true" />
            Restoring…
          </div>
        ) : (
          <form
            className="login-form"
            onSubmit={handleSubmit}
            noValidate
          >
            {demoAccounts.length > 0 && (
              <div
                className="login-demo-actions"
                aria-label="Choose an account"
              >
                {demoAccounts.map((account) => (
                  <button
                    key={account.label}
                    type="button"
                    className="login-demo-button"
                    aria-pressed={
                      username === account.username &&
                      password === account.password
                    }
                    disabled={submitting}
                    onClick={() => {
                      setUsername(account.username);
                      setPassword(account.password);
                      setError(null);
                    }}
                  >
                    {account.label}
                  </button>
                ))}
              </div>
            )}

            <div className="login-field">
              <label htmlFor="username">Username</label>
              <input
                id="username"
                name="username"
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                value={username}
                onChange={(event) => {
                  setUsername(event.target.value);
                  setError(null);
                }}
                disabled={submitting}
                aria-invalid={Boolean(error)}
                aria-describedby={
                  error ? "login-error" : undefined
                }
                autoFocus
              />
            </div>

            <div className="login-field">
              <label htmlFor="password">Password</label>
              <input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => {
                  setPassword(event.target.value);
                  setError(null);
                }}
                disabled={submitting}
                aria-invalid={Boolean(error)}
                aria-describedby={
                  error ? "login-error" : undefined
                }
              />
            </div>

            {error && (
              <div
                id="login-error"
                className="alert alert-error"
                role="alert"
              >
                {error}
              </div>
            )}

            <button
              type="submit"
              className="button button-primary button-wide"
              disabled={submitting}
              aria-live="polite"
            >
              {entering
                ? "Opening Stockpile…"
                : submitting
                  ? "Signing in…"
                : selectedAccount
                  ? `Continue as ${selectedAccount.label}`
                  : "Sign in"}
            </button>
          </form>
        )}
      </section>

      <footer className="site-footer login-footer">
        © 2026 Emman Ermitaño. All rights reserved.
      </footer>
    </main>
  );
}
