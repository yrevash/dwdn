import { execFile, spawn } from "child_process";
import { promisify } from "util";
import { existsSync, unlinkSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import { randomUUID } from "crypto";

const execFileAsync = promisify(execFile);

export interface VideoInfo {
  title: string;
  thumbnail: string;
  duration: number;
  uploader: string;
  platform: string;
  url: string;
  formats: FormatInfo[];
}

export interface FormatInfo {
  format_id: string;
  ext: string;
  resolution: string;
  filesize: number | null;
  vcodec: string;
  acodec: string;
}

function detectPlatform(url: string): string {
  if (/youtube\.com|youtu\.be/.test(url)) return "YouTube";
  if (/instagram\.com/.test(url)) return "Instagram";
  if (/tiktok\.com/.test(url)) return "TikTok";
  if (/twitter\.com|x\.com/.test(url)) return "Twitter/X";
  if (/facebook\.com|fb\.watch/.test(url)) return "Facebook";
  if (/reddit\.com/.test(url)) return "Reddit";
  if (/vimeo\.com/.test(url)) return "Vimeo";
  return "Video";
}

const YT_DLP_CANDIDATES = [
  "/opt/homebrew/bin/yt-dlp",
  "/usr/local/bin/yt-dlp",
  "/usr/bin/yt-dlp",
  "yt-dlp",
];

let cachedYtDlpPath: string | null = null;

async function findYtDlp(): Promise<string> {
  if (cachedYtDlpPath) return cachedYtDlpPath;
  for (const p of YT_DLP_CANDIDATES) {
    try {
      await execFileAsync(p, ["--version"], { timeout: 5000 });
      cachedYtDlpPath = p;
      return p;
    } catch {}
  }
  throw new Error("yt-dlp not found. Run: brew install yt-dlp");
}

export async function getVideoInfo(url: string): Promise<VideoInfo> {
  const ytDlp = await findYtDlp();

  const { stdout } = await execFileAsync(
    ytDlp,
    ["--dump-json", "--no-playlist", "--no-warnings", url],
    { timeout: 30000 }
  );

  const data = JSON.parse(stdout);

  return {
    title: data.title || "Unknown Title",
    thumbnail: data.thumbnail || "",
    duration: data.duration || 0,
    uploader: data.uploader || data.channel || "Unknown",
    platform: detectPlatform(url),
    url,
    formats: ((data.formats || []) as Record<string, unknown>[])
      .filter((f) => f.vcodec !== "none" || f.acodec !== "none")
      .map((f) => ({
        format_id: String(f.format_id),
        ext: String(f.ext),
        resolution: String(f.resolution || (f.height ? `${f.height}p` : "audio only")),
        filesize: (f.filesize as number) || (f.filesize_approx as number) || null,
        vcodec: String(f.vcodec),
        acodec: String(f.acodec),
      }))
      .slice(-10),
  };
}

export interface DownloadResult {
  filePath: string;
  filename: string;
  cleanup: () => void;
}

export async function downloadVideo(url: string): Promise<DownloadResult> {
  const ytDlp = await findYtDlp();
  const id = randomUUID();
  const outputTemplate = join(tmpdir(), `dwdn-${id}.%(ext)s`);

  // Best quality: merge best video + best audio into mp4
  const format =
    "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best";

  await new Promise<void>((resolve, reject) => {
    const proc = spawn(ytDlp, [
      "--no-playlist",
      "--no-warnings",
      "-f",
      format,
      "--merge-output-format",
      "mp4",
      "--output",
      outputTemplate,
      "--no-part",
      url,
    ]);

    let stderr = "";
    proc.stderr.on("data", (chunk: Buffer) => {
      stderr += chunk.toString();
    });
    proc.on("close", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`yt-dlp failed (code ${code}): ${stderr.slice(-500)}`));
    });
    proc.on("error", reject);
  });

  // Find the output file (yt-dlp fills in the extension)
  const possibleExts = ["mp4", "mkv", "webm", "mov", "avi"];
  let filePath: string | null = null;

  for (const ext of possibleExts) {
    const candidate = join(tmpdir(), `dwdn-${id}.${ext}`);
    if (existsSync(candidate)) {
      filePath = candidate;
      break;
    }
  }

  if (!filePath) throw new Error("Download completed but output file not found");

  const filename = `video-${id}.mp4`;
  const resolvedPath = filePath;

  return {
    filePath: resolvedPath,
    filename,
    cleanup: () => {
      try {
        if (existsSync(resolvedPath)) unlinkSync(resolvedPath);
      } catch {}
    },
  };
}
