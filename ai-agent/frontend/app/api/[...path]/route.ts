import { NextRequest, NextResponse } from "next/server";

// Proxies the browser's relative /api/* calls to the real backend, server-side. Deliberately a
// Route Handler (not next.config.ts's rewrites()) - rewrites() gets resolved into the standalone
// build's routing manifest at `next build` time, so it never sees a runtime-only BACKEND_URL (a
// real deployed test caught this: the proxy kept hitting the build-time default instead of the
// actual backend Service). A Route Handler runs per-request on the server, so it reads
// process.env fresh every time - this is what makes BACKEND_URL truly runtime-configurable.
function backendUrl(): string {
  return process.env.BACKEND_URL || "http://127.0.0.1:8000";
}

async function proxy(req: NextRequest, path: string[]): Promise<NextResponse> {
  const target = `${backendUrl()}/api/${path.join("/")}${req.nextUrl.search}`;
  const init: RequestInit = {
    method: req.method,
    headers: { "Content-Type": req.headers.get("content-type") || "application/json" },
  };
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = await req.text();
  }
  try {
    const res = await fetch(target, init);
    const body = await res.text();
    return new NextResponse(body, {
      status: res.status,
      headers: { "Content-Type": res.headers.get("content-type") || "application/json" },
    });
  } catch (err) {
    return NextResponse.json(
      { error: `Failed to reach backend at ${backendUrl()}: ${(err as Error).message}` },
      { status: 502 }
    );
  }
}

type RouteParams = { params: Promise<{ path: string[] }> };

export async function GET(req: NextRequest, { params }: RouteParams) {
  return proxy(req, (await params).path);
}

export async function POST(req: NextRequest, { params }: RouteParams) {
  return proxy(req, (await params).path);
}
