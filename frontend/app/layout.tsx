import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "Stockpile",
  description:
    "Barcode inventory and stock location tracking.",
  icons: {
    icon: "/app-logo.png",
    apple: "/app-logo.png",
  },
};

export default function RootLayout({
  children,
}: {
  children: ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}