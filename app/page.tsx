"use client";

import { useState, useRef, useEffect } from "react";
import Image from "next/image";

interface VideoInfo {
  title: string;
  thumbnail: string;
  duration: number;
  uploader: string;
  platform: string;
  url: string;
}

type State = "idle" | "fetching" | "ready" | "downloading" | "done" | "error";

function formatDuration(seconds: number): string {
  if (!seconds) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

const PLATFORM_COLORS: Record<string, string> = {
  YouTube: "bg-red-600",
  Instagram: "bg-pink-600",
  TikTok: "bg-cyan-600",
  "Twitter/X": "bg-neutral-600",
  Facebook: "bg-blue-700",
  Reddit: "bg-orange-600",
  Vimeo: "bg-sky-600",
  Video: "bg-neutral-600",
};

function PlatformBadge({ platform }: { platform: string }) {
  return (
    <span
      className={`text-xs px-2 py-0.5 rounded-full font-medium ${
        PLATFORM_COLORS[platform] ?? "bg-neutral-600"
      }`}
    >
      {platform}
    </span>
  );
}

function Spinner() {
  return (
    <div className="w-4 h-4 border-2 border-neutral-700 border-t-neutral-400 rounded-full animate-spin" />
  );
}

export default function Home() {
  const [url, setUrl] = useState("");
  const [state, setState] = useState<State>("idle");
  const [info, setInfo] = useState<VideoInfo | null>(null);
  const [error, setError] = useState("");
  const [progress, setProgress] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  async function fetchInfo(rawUrl: string) {
    const trimmed = rawUrl.trim();
    if (!trimmed) return;

    setState("fetching");
    setInfo(null);
    setError("");

    try {
      const res = await fetch("/api/info", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: trimmed }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to fetch video info");
      setInfo(data);
      setState("ready");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
      setState("error");
    }
  }

  async function handleDownload() {
    if (!info) return;

    setState("downloading");
    setProgress(0);

    const ab = new AbortController();
    abortRef.current = ab;

    try {
      const res = await fetch(`/api/download?url=${encodeURIComponent(info.url)}`, {
        signal: ab.signal,
      });

      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.error || "Download failed");
      }

      const contentLength = res.headers.get("content-length");
      const total = contentLength ? parseInt(contentLength, 10) : 0;
      const reader = res.body!.getReader();
      const chunks: Uint8Array[] = [];
      let received = 0;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        received += value.length;
        if (total > 0) setProgress(Math.round((received / total) * 100));
      }

      const blob = new Blob(chunks as BlobPart[], { type: "video/mp4" });
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = blobUrl;
      a.download = `${info.title
        .replace(/[^\w\s\-]/g, "")
        .trim()
        .slice(0, 80)}.mp4`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(blobUrl);

      setState("done");
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        setState("ready");
        return;
      }
      setError(err instanceof Error ? err.message : "Download failed");
      setState("error");
    }
  }

  function reset() {
    abortRef.current?.abort();
    setUrl("");
    setInfo(null);
    setError("");
    setState("idle");
    setProgress(0);
    setTimeout(() => inputRef.current?.focus(), 50);
  }

  function handlePaste(e: React.ClipboardEvent<HTMLInputElement>) {
    const pasted = e.clipboardData.getData("text").trim();
    if (pasted.startsWith("http://") || pasted.startsWith("https://")) {
      setUrl(pasted);
      setTimeout(() => fetchInfo(pasted), 0);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") fetchInfo(url);
  }

  const isBusy = state === "fetching" || state === "downloading";

  return (
    <main className="min-h-screen flex flex-col items-center justify-center px-4 py-16">
      <div className="w-full max-w-xl space-y-8">
        {/* Header */}
        <div className="text-center space-y-1">
          <h1 className="text-3xl font-bold tracking-tight">dwdn</h1>
          <p className="text-neutral-500 text-sm">Paste any video link to download at highest quality</p>
        </div>

        {/* Input */}
        <div className="relative">
          <input
            ref={inputRef}
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onPaste={handlePaste}
            onKeyDown={handleKeyDown}
            placeholder="https://youtube.com/shorts/... or instagram.com/reel/..."
            disabled={isBusy}
            className="w-full bg-neutral-900 border border-neutral-800 rounded-xl px-4 py-4 pr-24 text-sm placeholder-neutral-600 focus:outline-none focus:border-neutral-600 disabled:opacity-50 transition-colors"
          />
          <button
            onClick={() => fetchInfo(url)}
            disabled={!url.trim() || isBusy}
            className="absolute right-2 top-1/2 -translate-y-1/2 bg-white text-black text-sm font-semibold px-4 py-2 rounded-lg disabled:opacity-30 hover:bg-neutral-200 active:bg-neutral-300 transition-colors"
          >
            {state === "fetching" ? "..." : "Fetch"}
          </button>
        </div>

        {/* Fetching indicator */}
        {state === "fetching" && (
          <div className="flex items-center justify-center gap-2 text-neutral-500 text-sm">
            <Spinner />
            Fetching video info...
          </div>
        )}

        {/* Error */}
        {state === "error" && (
          <div className="bg-red-950 border border-red-900 rounded-xl p-4 text-sm">
            <p className="font-medium text-red-400 mb-1">Error</p>
            <p className="text-red-500 text-xs leading-relaxed">{error}</p>
            <button
              onClick={reset}
              className="mt-3 text-red-400 hover:text-red-300 underline text-xs transition-colors"
            >
              Try again
            </button>
          </div>
        )}

        {/* Video Info Card */}
        {info && (state === "ready" || state === "downloading" || state === "done") && (
          <div className="bg-neutral-900 border border-neutral-800 rounded-xl overflow-hidden">
            {/* Thumbnail */}
            {info.thumbnail && (
              <div className="relative aspect-video bg-neutral-950">
                <Image
                  src={info.thumbnail}
                  alt={info.title}
                  fill
                  className="object-cover"
                  unoptimized
                />
              </div>
            )}

            <div className="p-4 space-y-3">
              {/* Meta */}
              <div className="flex items-center gap-2">
                <PlatformBadge platform={info.platform} />
                {info.duration > 0 && (
                  <span className="text-xs text-neutral-500">
                    {formatDuration(info.duration)}
                  </span>
                )}
              </div>

              {/* Title */}
              <p className="text-sm font-medium leading-snug line-clamp-2">{info.title}</p>
              <p className="text-xs text-neutral-500">{info.uploader}</p>

              {/* Actions */}
              {state === "ready" && (
                <button
                  onClick={handleDownload}
                  className="w-full bg-white text-black font-semibold py-3 rounded-lg hover:bg-neutral-200 active:bg-neutral-300 transition-colors text-sm"
                >
                  Download (Best Quality)
                </button>
              )}

              {state === "downloading" && (
                <div className="space-y-2">
                  <div className="flex justify-between text-xs text-neutral-500">
                    <span className="flex items-center gap-1.5">
                      <Spinner />
                      Downloading...
                    </span>
                    <span>{progress > 0 ? `${progress}%` : "Processing"}</span>
                  </div>
                  <div className="h-1.5 bg-neutral-800 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-white rounded-full transition-all duration-300"
                      style={{
                        width: progress > 0 ? `${progress}%` : "30%",
                        animation:
                          progress === 0 ? "pulse 1.5s ease-in-out infinite" : "none",
                      }}
                    />
                  </div>
                  <button
                    onClick={() => {
                      abortRef.current?.abort();
                      setState("ready");
                    }}
                    className="w-full text-neutral-600 hover:text-neutral-400 text-xs transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              )}

              {state === "done" && (
                <div className="space-y-2">
                  <div className="w-full bg-neutral-800 text-neutral-300 font-medium py-3 rounded-lg text-sm text-center">
                    Downloaded
                  </div>
                  <button
                    onClick={reset}
                    className="w-full text-neutral-600 hover:text-neutral-400 text-xs transition-colors"
                  >
                    Download another
                  </button>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Supported platforms hint */}
        {state === "idle" && (
          <p className="text-center text-neutral-700 text-xs">
            YouTube · Instagram · TikTok · Twitter/X · Facebook · Reddit · Vimeo · 1000+ sites
          </p>
        )}
      </div>
    </main>
  );
}
