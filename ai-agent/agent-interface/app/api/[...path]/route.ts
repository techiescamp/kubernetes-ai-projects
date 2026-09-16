import { NextRequest, NextResponse } from "next/server";

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
