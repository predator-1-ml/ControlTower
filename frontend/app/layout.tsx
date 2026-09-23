import type { Metadata } from "next";
import { Public_Sans } from "next/font/google";
import "./globals.css";

// ONE family for everything: headings, labels, chips, conversation. A product UI
// does not need a display/body pairing, and a second face is a second thing to
// explain. Public Sans was drawn for government service interfaces: neutral,
// sturdy at 12-14px (where chips and the activity log sit), and its digits have
// a tabular set, which the ids and clock times need to line up.
//
// Rejected: a separate mono for ids. "Monospace because it is technical" is a
// costume; the ids only need digits that line up, which `tabular-nums` gives.
// Rejected: Inter and Geist — the faces every AI tool ships in.
//
// next/font downloads the file at BUILD time and serves it from this origin, so
// an internal user's browser never calls Google — which matters for an app whose
// deployment story is "nothing leaves the VPC". The cost: `next build` needs
// outbound HTTPS to fonts.googleapis.com.
const publicSans = Public_Sans({
  subsets: ["latin"],
  variable: "--font-public-sans",
});

export const metadata: Metadata = {
  title: "Control Tower",
  description: "Coordinate onboarding, claims and policy workflows from one conversation.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={publicSans.variable}>
      <body className="bg-canvas font-sans text-ink antialiased">{children}</body>
    </html>
  );
}
