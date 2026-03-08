import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "dwdn – Video Downloader",
  description: "Download YouTube Shorts, Instagram Reels, and more in highest quality",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-black text-white antialiased">{children}</body>
    </html>
  );
}
