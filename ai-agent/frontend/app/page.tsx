"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface Message {
  role: "user" | "ai";
  text: string;
  goalBadge?: "achieved" | "not-achieved";
}

interface UsageRole {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cost: number | null;
}

interface UsageData {
  roles: Record<string, UsageRole>;
  total_cost: number | null;
}

type Phase = "idle" | "busy" | "awaiting_decision" | "awaiting_retry";

const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || "http://127.0.0.1:8000";

export default function Home() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [busyText, setBusyText] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [usage, setUsage] = useState<UsageData | null>(null);

  const messagesEndRef = useRef<HTMLDivElement>(null);

  const fetchUsage = useCallback(async () => {
    try {
      const res = await fetch(`${backendUrl}/api/usage`);
      const data = await res.json();
      setUsage(data);
    } catch (err) {
      console.error("Failed to fetch usage:", err);
    }
  }, []);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, phase]);

  useEffect(() => {
    const timeout = setTimeout(() => fetchUsage(), 0);
    return () => clearTimeout(timeout);
  }, [fetchUsage]);

  const addMessage = (msg: Message) => setMessages((prev) => [...prev, msg]);

  const handleSend = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || phase === "busy") return;

    const query = input.trim();
    setInput("");
    addMessage({ role: "user", text: query });
    setPhase("busy");
    setBusyText("Diagnosing (Nova Pro)...");

    try {
      const res = await fetch(`${backendUrl}/api/query`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      if (!res.ok) throw new Error("Backend query failed");
      const data = await res.json();

      if (!data.remediation_needed) {
        addMessage({ role: "ai", text: data.diagnostic_report });
        setPhase("idle");
        fetchUsage();
        return;
      }

      addMessage({
        role: "ai",
        text: `### Diagnostic Report\n\n${data.diagnostic_report}\n\n### Proposed Remediation\n\n${data.proposed_fix}`,
      });
      setSessionId(data.session_id);
      setPhase("awaiting_decision");
      fetchUsage();
    } catch (err) {
      console.error(err);
      addMessage({
        role: "ai",
        text: "Sorry, I couldn't reach the backend. Make sure `python main.py` is running.",
      });
      setPhase("idle");
    }
  };

  const handleDecision = async (approved: boolean) => {
    if (!sessionId) return;
    setPhase("busy");
    setBusyText(approved ? "Applying remediation (Llama 4)..." : "Cancelling...");

    try {
      const res = await fetch(`${backendUrl}/api/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, approved }),
      });
      if (!res.ok) throw new Error("Decision request failed");
      const data = await res.json();

      if (data.status === "cancelled") {
        addMessage({ role: "ai", text: "Remediation cancelled by user." });
        setSessionId(null);
        setPhase("idle");
        fetchUsage();
        return;
      }

      const resultText = `### Remediation Result\n\n${data.fix_result}\n\n### Verification (Nova Pro)\n\n${data.verification}`;

      if (data.status === "done") {
        addMessage({
          role: "ai",
          text: resultText,
          goalBadge: data.goal_achieved ? "achieved" : "not-achieved",
        });
        setSessionId(null);
        setPhase("idle");
      } else {
        // retry_available
        addMessage({ role: "ai", text: resultText, goalBadge: "not-achieved" });
        setPhase("awaiting_retry");
      }
      fetchUsage();
    } catch (err) {
      console.error(err);
      addMessage({ role: "ai", text: "Something went wrong applying the fix." });
      setPhase("idle");
    }
  };

  const handleRetry = async (retry: boolean) => {
    if (!sessionId) return;
    setPhase("busy");
    setBusyText(retry ? "Proposing a new fix (Llama 4)..." : "Stopping...");

    try {
      const res = await fetch(`${backendUrl}/api/retry`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, retry }),
      });
      if (!res.ok) throw new Error("Retry request failed");
      const data = await res.json();

      if (data.status === "stopped") {
        addMessage({ role: "ai", text: "Stopped - goal was not confirmed achieved." });
        setSessionId(null);
        setPhase("idle");
        fetchUsage();
        return;
      }

      addMessage({
        role: "ai",
        text: `### New Proposed Remediation (attempt ${data.attempt + 1}/${data.max_attempts})\n\n${data.proposed_fix}`,
      });
      setPhase("awaiting_decision");
      fetchUsage();
    } catch (err) {
      console.error(err);
      addMessage({ role: "ai", text: "Something went wrong proposing a new fix." });
      setPhase("idle");
    }
  };

  return (
    <div className="app-container">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-logo">D</div>
          <div>
            <h1 className="brand-name">DocuMancer AI</h1>
            <p style={{ fontSize: "11px", color: "var(--text-muted)", fontWeight: 600, textTransform: "uppercase" }}>
              K8s Diagnosis & Remediation
            </p>
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column" }}>
          <div className="info-row">Diagnostics Model</div>
          <div className="info-value">Amazon Nova Pro</div>
          <div className="info-row">Remediation Model</div>
          <div className="info-value">Meta Llama 4 Maverick</div>
        </div>

        <div style={{ marginTop: "auto", borderTop: "1px solid var(--panel-border)", paddingTop: "16px" }}>
          <div className="info-row">Token Usage (session)</div>
          {usage ? (
            <>
              {Object.entries(usage.roles).map(([role, r]) => (
                <div key={role} className="usage-line">
                  <span style={{ textTransform: "capitalize" }}>{role}</span>
                  <span>
                    {r.input_tokens + r.output_tokens} tok / {r.calls} calls
                  </span>
                </div>
              ))}
              <div className="usage-line" style={{ fontWeight: 600, color: "var(--text-primary)" }}>
                <span>Estimated cost</span>
                <span>{usage.total_cost !== null ? `$${usage.total_cost.toFixed(4)}` : "n/a"}</span>
              </div>
            </>
          ) : (
            <div className="info-value">Loading...</div>
          )}
        </div>
      </aside>

      <main className="main-chat">
        <header className="chat-header">
          <span
            style={{
              fontSize: "12px",
              padding: "4px 8px",
              borderRadius: "8px",
              background: "rgba(255,255,255,0.05)",
              color: "var(--text-secondary)",
              border: "1px solid var(--panel-border)",
            }}
          >
            Reason - Act - Verify
          </span>
        </header>

        <div className="chat-messages">
          {messages.length === 0 ? (
            <div className="empty-state">
              <div className="empty-icon">⎈</div>
              <h3 style={{ fontSize: "18px", color: "var(--text-primary)" }}>Describe a cluster issue</h3>
              <p style={{ fontSize: "14px", color: "var(--text-secondary)" }}>
                e.g. &ldquo;my pod api-server-abc123 is crash looping, can you fix it?&rdquo;
              </p>
            </div>
          ) : (
            messages.map((msg, idx) => (
              <div key={idx} className={`message-row ${msg.role} animate-message`}>
                <div className="message-bubble">
                  <div className="message-sender">{msg.role === "user" ? "You" : "DocuMancer Engine"}</div>

                  {msg.goalBadge && (
                    <div className={`goal-badge ${msg.goalBadge}`}>
                      {msg.goalBadge === "achieved" ? "Goal Achieved" : "Goal Not Confirmed"}
                    </div>
                  )}

                  <div className="markdown-body">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.text}</ReactMarkdown>
                  </div>

                  {phase === "awaiting_decision" && idx === messages.length - 1 && (
                    <div className="decision-actions">
                      <button className="decision-btn approve" onClick={() => handleDecision(true)}>
                        Approve & Apply
                      </button>
                      <button className="decision-btn reject" onClick={() => handleDecision(false)}>
                        Reject
                      </button>
                    </div>
                  )}

                  {phase === "awaiting_retry" && idx === messages.length - 1 && (
                    <div className="decision-actions">
                      <button className="decision-btn approve" onClick={() => handleRetry(true)}>
                        Try New Fix
                      </button>
                      <button className="decision-btn reject" onClick={() => handleRetry(false)}>
                        Stop
                      </button>
                    </div>
                  )}
                </div>
              </div>
            ))
          )}

          {phase === "busy" && (
            <div className="message-row ai animate-message">
              <div className="message-bubble" style={{ color: "var(--text-muted)" }}>
                <div className="message-sender">DocuMancer Engine</div>
                <div style={{ display: "flex", gap: "4px", alignItems: "center" }}>
                  <span>{busyText}</span>
                  <div className="status-dot running" style={{ width: "6px", height: "6px" }} />
                </div>
              </div>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        <div className="chat-input-container">
          <form onSubmit={handleSend} className="chat-input-form">
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Describe the Kubernetes issue or ask a question..."
              className="chat-input"
              disabled={phase !== "idle"}
            />
            <button type="submit" disabled={phase !== "idle" || !input.trim()} className="chat-send-btn">
              &#10148;
            </button>
          </form>
        </div>
      </main>
    </div>
  );
}
