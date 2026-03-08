import { NextRequest, NextResponse } from "next/server";
import { getVideoInfo } from "@/lib/downloader";

export async function POST(req: NextRequest) {
  try {
    const { url } = await req.json();

    if (!url || typeof url !== "string") {
      return NextResponse.json({ error: "URL is required" }, { status: 400 });
    }

    const trimmed = url.trim();
    if (!trimmed.startsWith("http://") && !trimmed.startsWith("https://")) {
      return NextResponse.json({ error: "Invalid URL – must start with http:// or https://" }, { status: 400 });
    }

    const info = await getVideoInfo(trimmed);
    return NextResponse.json(info);
  } catch (err) {
    const message = err instanceof Error ? err.message : "Failed to fetch video info";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
