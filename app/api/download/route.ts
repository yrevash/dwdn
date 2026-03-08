import { NextRequest, NextResponse } from "next/server";
import { downloadVideo } from "@/lib/downloader";
import { createReadStream, statSync } from "fs";
import { Readable } from "stream";

export const maxDuration = 300; // 5 min for large videos

export async function GET(req: NextRequest) {
  const url = req.nextUrl.searchParams.get("url");

  if (!url) {
    return NextResponse.json({ error: "URL is required" }, { status: 400 });
  }

  const trimmed = decodeURIComponent(url).trim();
  if (!trimmed.startsWith("http://") && !trimmed.startsWith("https://")) {
    return NextResponse.json({ error: "Invalid URL" }, { status: 400 });
  }

  try {
    const { filePath, filename, cleanup } = await downloadVideo(trimmed);

    const stat = statSync(filePath);
    const nodeStream = createReadStream(filePath);
    const webStream = Readable.toWeb(nodeStream) as ReadableStream;

    nodeStream.on("close", cleanup);

    return new NextResponse(webStream, {
      headers: {
        "Content-Type": "video/mp4",
        "Content-Disposition": `attachment; filename="${filename}"`,
        "Content-Length": stat.size.toString(),
        "Cache-Control": "no-cache, no-store",
        "X-Content-Type-Options": "nosniff",
      },
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Download failed";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
