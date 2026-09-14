"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface Message {
  role: "user" | "ai";
  text: string;
  goalBadge?: "achieved" | "not-achieved";
}

interface AttemptRecord {
  attempt: number;
  proposed_fix: string;
  fix_result: string;
  verification: string;
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

type Phase = "idle" | "busy" | "awaiting_decision" | "awaiting_retry" | "awaiting_issue_selection";

// Relative paths only - proxied server-side to the real backend by the rewrite in
// next.config.ts, so this works from the browser regardless of how the frontend itself was
// reached (port-forward, LoadBalancer, Ingress - the origin doesn't matter).
const backendUrl = "";

export default function Home() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [busyText, setBusyText] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [usage, setUsage] = useState<UsageData | null>(null);
  const [guidanceInput, setGuidanceInput] = useState("");
  const [issues, setIssues] = useState<string[]>([]);
  const [selectedIssues, setSelectedIssues] = useState<Set<number>>(new Set());
  const [etaText, setEtaText] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [theme, setTheme] = useState<"light" | "dark">("dark");

  const messagesEndRef = useRef<HTMLDivElement>(null);
  // Lets the Cancel button abort the in-flight request and hand the UI back to the user.
  const abortRef = useRef<AbortController | null>(null);

  // Live elapsed-seconds counter, shown next to the ETA while a step is running.
  useEffect(() => {
    if (phase !== "busy") {
      setElapsed(0);
      return;
    }
    const started = Date.now();
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(id);
  }, [phase]);

  const beginBusy = (text: string, eta: string) => {
    setBusyText(text);
    setEtaText(eta);
    setPhase("busy");
    abortRef.current = new AbortController();
    return abortRef.current.signal;
  };

  const handleCancel = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    addMessage({
      role: "ai",
      text:
        "Cancelled. Note: a cluster action that was already sent may still finish on the server - " +
        "re-check the resource before assuming nothing changed.",
    });
    setPhase("idle");
  };

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

  // Restore the saved theme, falling back to the OS preference on first visit.
  useEffect(() => {
    const saved = localStorage.getItem("kubecheck-theme");
    if (saved === "light" || saved === "dark") {
      setTheme(saved);
    } else {
      setTheme(window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
    }
  }, []);

  // Everything is driven off tokens scoped to [data-theme], so setting this one
  // attribute restyles the whole interface.
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("kubecheck-theme", theme);
  }, [theme]);

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

    // Multi-task: a new question while an earlier fix is still awaiting your decision simply
    // abandons that pending proposal and starts fresh, instead of the input staying locked until
    // you approve or reject it. Nothing was applied for the abandoned one - it only ever paused
    // before the write step.
    if (sessionId && (phase === "awaiting_decision" || phase === "awaiting_retry" || phase === "awaiting_issue_selection")) {
      addMessage({
        role: "ai",
        text: "_Previous proposal dropped (nothing was applied) - starting the new request._",
      });
      setSessionId(null);
      setIssues([]);
      setSelectedIssues(new Set());
    }

    const signal = beginBusy("Diagnosing the cluster...", "usually 30-60s");

    try {
      const res = await fetch(`${backendUrl}/api/query`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
        signal,
      });
      if (!res.ok) throw new Error("Backend query failed");
      const data = await res.json();

      if (!data.remediation_needed) {
        addMessage({ role: "ai", text: data.diagnostic_report });
        setPhase("idle");
        fetchUsage();
        return;
      }

      if (data.needs_issue_selection) {
        addMessage({
          role: "ai",
          text: `### Diagnostic Report\n\n${data.diagnostic_report}\n\nFound ${data.issues.length} separate issues - pick which one(s) to fix below.`,
        });
        setIssues(data.issues);
        setSelectedIssues(new Set(data.issues.map((_: string, i: number) => i)));
        setSessionId(data.session_id);
        setPhase("awaiting_issue_selection");
        fetchUsage();
        return;
      }

      if (data.auto_approved) {
        // REQUIRE_APPROVAL=false on the backend - the fix already ran (and was verified) with no
        // approval step, so there's no session to act on - show the complete outcome directly.
        addMessage({
          role: "ai",
          text: data.goal_achieved
            ? `### Fixed\n\n${data.verification}`
            : `### Not fixed\n\n${data.verification}\n\n**What was tried**\n\n${data.fix_result}`,
          goalBadge: data.goal_achieved ? "achieved" : "not-achieved",
        });
        setPhase("idle");
        fetchUsage();
        return;
      }

      addMessage({
        role: "ai",
        text: `${data.diagnostic_report}\n\n### Proposed Fix\n\n${data.proposed_fix}`,
      });
      setSessionId(data.session_id);
      setPhase("awaiting_decision");
      fetchUsage();
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return; // handleCancel already reported it
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
    const signal = beginBusy(
      approved ? "Applying the fix and verifying it..." : "Cancelling...",
      approved ? "usually 1-3 min (retries automatically up to 3x)" : "a few seconds"
    );

    try {
      const res = await fetch(`${backendUrl}/api/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, approved }),
        signal,
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

      // Lead with the VERIFIED outcome, not the intermediate "actions were applied" step - that
      // read like a success banner while the real answer sat underneath it. What actually changed
      // goes below, as supporting detail.
      // On success the verification sentence already states what changed, so the raw tool-call
      // list is redundant noise (and included failed-but-harmless detours like a 404 or a 409 on
      // an existing namespace, which look alarming next to a "Fixed" heading). Only show what was
      // tried when it did NOT work, where it's the useful part.
      const resultText = data.goal_achieved
        ? `### Fixed\n\n${data.verification}`
        : `### Not fixed\n\n${data.verification}\n\n**What was tried**\n\n${data.fix_result}`;

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
      if ((err as Error)?.name === "AbortError") return;
      console.error(err);
      addMessage({ role: "ai", text: "Something went wrong applying the fix." });
      setPhase("idle");
    }
  };

  const handleRetry = async (retry: boolean) => {
    if (!sessionId) return;
    const signal = beginBusy(
      retry ? "Proposing a new fix..." : "Stopping...",
      retry ? "usually 20-40s" : "a few seconds"
    );

    try {
      const res = await fetch(`${backendUrl}/api/retry`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, retry }),
        signal,
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
      if ((err as Error)?.name === "AbortError") return;
      console.error(err);
      addMessage({ role: "ai", text: "Something went wrong proposing a new fix." });
      setPhase("idle");
    }
  };

  const handleGuidance = async (e: React.FormEvent) => {
    e.preventDefault();
    const instruction = guidanceInput.trim();
    if (!instruction || !sessionId) return;

    setGuidanceInput("");
    addMessage({ role: "user", text: instruction });
    const signal = beginBusy("Rethinking the fix with your instruction...", "usually 20-40s");

    try {
      const res = await fetch(`${backendUrl}/api/guidance`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, instruction }),
        signal,
      });
      if (!res.ok) throw new Error("Guidance request failed");
      const data = await res.json();

      addMessage({
        role: "ai",
        text: `### Updated Proposed Remediation (attempt ${data.attempt + 1}/${data.max_attempts})\n\n${data.proposed_fix}`,
      });
      setPhase("awaiting_decision");
      fetchUsage();
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      console.error(err);
      addMessage({ role: "ai", text: "Something went wrong applying your instruction." });
      setPhase("idle");
    }
  };

  const handleIssueSelection = async () => {
    if (!sessionId || selectedIssues.size === 0) return;
    const signal = beginBusy(
      `Planning a fix for ${selectedIssues.size} selected issue(s)...`,
      "usually 20-40s"
    );

    try {
      const res = await fetch(`${backendUrl}/api/select-issues`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, selected_indices: Array.from(selectedIssues) }),
        signal,
      });
      if (!res.ok) throw new Error("Issue selection request failed");
      const data = await res.json();

      addMessage({
        role: "ai",
        text: `### Proposed Remediation\n\n${data.proposed_fix}`,
      });
      setIssues([]);
      setSelectedIssues(new Set());
      setPhase("awaiting_decision");
      fetchUsage();
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      console.error(err);
      addMessage({ role: "ai", text: "Something went wrong proposing a fix for the selected issue(s)." });
      setPhase("idle");
    }
  };

  const toggleIssue = (idx: number) => {
    setSelectedIssues((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  };

  // Before the first message the composer sits centred under a greeting; once the conversation
  // starts it docks to the bottom. Same form either way - only its container changes.
  const hasStarted = messages.length > 0 || phase === "busy";

  const composer = (
    <div className="composer">
      <form onSubmit={handleSend} className="chat-input-form">
        <textarea
          value={input}
          onChange={(e) => {
            setInput(e.target.value);
            // Grow to fit the typed lines, capped by max-height in CSS.
            e.target.style.height = "auto";
            e.target.style.height = `${Math.min(e.target.scrollHeight, 180)}px`;
          }}
          onKeyDown={(e) => {
            // Enter sends, Shift+Enter inserts a newline - so multi-line input (a YAML
            // snippet, a multi-step request) can be typed without submitting halfway through.
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              handleSend(e);
            }
          }}
          rows={1}
          placeholder={
            phase === "busy"
              ? "Working… cancel above to type a new request"
              : hasStarted
                ? "Ask something else, or describe another problem…"
                : "Describe the problem, or ask about the cluster…"
          }
          className="chat-input"
          // Only locked while a request is actually in flight. A pending approval no longer
          // blocks you from asking something else - sending a new query drops that proposal.
          disabled={phase === "busy"}
        />
        <button type="submit" disabled={phase === "busy" || !input.trim()} className="chat-send-btn">
          Send
        </button>
      </form>
    </div>
  );

  return (
    <div className="app-container">
      <aside className="sidebar">
        <div className="brand">
          <h1 className="brand-name">KubeCheck</h1>
        </div>

        <div className="usage-block">
          <div className="side-heading">This session</div>
          {usage ? (
            <>
              {Object.entries(usage.roles).map(([role, r]) => (
                <div key={role} className="usage-line">
                  <span>{role}</span>
                  <span>{(r.input_tokens + r.output_tokens).toLocaleString()} tok</span>
                </div>
              ))}
              <div className="usage-line usage-total">
                <span>cost</span>
                <span>{usage.total_cost !== null ? `$${usage.total_cost.toFixed(4)}` : "n/a"}</span>
              </div>
            </>
          ) : (
            <div className="usage-line">
              <span>loading</span>
            </div>
          )}
        </div>

        <button
          className="theme-toggle"
          onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
        >
          {theme === "dark" ? "Light theme" : "Dark theme"}
        </button>
      </aside>

      <main className="main-chat">
        {!hasStarted && (
          <div className="hero">
            <h2 className="hero-greeting">How can I help?</h2>
            {composer}
          </div>
        )}

        {hasStarted && (
        <div className="chat-messages">
          {
            messages.map((msg, idx) => (
              <div key={idx} className={`message-row ${msg.role} animate-message`}>
                <div className="message-bubble">
                  <div className="message-sender">{msg.role === "user" ? "You" : "KubeCheck"}</div>

                  {msg.goalBadge && (
                    <div className={`goal-badge ${msg.goalBadge}`}>
                      {msg.goalBadge === "achieved" ? "Goal Achieved" : "Goal Not Confirmed"}
                    </div>
                  )}

                  <div className="markdown-body">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.text}</ReactMarkdown>
                  </div>

                  {phase === "awaiting_decision" && idx === messages.length - 1 && (
                    <div className="decision-panel">
                      <div className="decision-prompt">Nothing has changed on the cluster yet.</div>
                      <div className="decision-actions">
                        <button className="decision-btn approve" onClick={() => handleDecision(true)}>
                          Apply this fix
                        </button>
                        <button className="decision-btn reject" onClick={() => handleDecision(false)}>
                          Discard
                        </button>
                      </div>
                      <form onSubmit={handleGuidance} className="guidance-form">
                        <input
                          type="text"
                          value={guidanceInput}
                          onChange={(ev) => setGuidanceInput(ev.target.value)}
                          placeholder="or tell it to do something different…"
                          className="guidance-input"
                        />
                        <button type="submit" disabled={!guidanceInput.trim()} className="guidance-send-btn">
                          Send
                        </button>
                      </form>
                    </div>
                  )}

                  {phase === "awaiting_retry" && idx === messages.length - 1 && (
                    <div className="decision-panel">
                      <div className="decision-prompt">That did not resolve it.</div>
                      <div className="decision-actions">
                        <button className="decision-btn approve" onClick={() => handleRetry(true)}>
                          Try a different fix
                        </button>
                        <button className="decision-btn reject" onClick={() => handleRetry(false)}>
                          Stop here
                        </button>
                      </div>
                      <form onSubmit={handleGuidance} className="guidance-form">
                        <input
                          type="text"
                          value={guidanceInput}
                          onChange={(ev) => setGuidanceInput(ev.target.value)}
                          placeholder="or tell it what to try instead…"
                          className="guidance-input"
                        />
                        <button type="submit" disabled={!guidanceInput.trim()} className="guidance-send-btn">
                          Send
                        </button>
                      </form>
                    </div>
                  )}

                  {phase === "awaiting_issue_selection" && idx === messages.length - 1 && (
                    <div className="issue-select-panel">
                      <div className="decision-prompt">Which should it fix?</div>
                      {issues.map((issue, i) => (
                        <label key={i} className="issue-checkbox-row">
                          <input
                            type="checkbox"
                            checked={selectedIssues.has(i)}
                            onChange={() => toggleIssue(i)}
                          />
                          <span>{issue}</span>
                        </label>
                      ))}
                      <div className="decision-actions">
                        <button
                          className="decision-btn approve"
                          disabled={selectedIssues.size === 0}
                          onClick={handleIssueSelection}
                        >
                          {selectedIssues.size === issues.length
                            ? `Fix all ${issues.length}`
                            : `Fix ${selectedIssues.size} of ${issues.length}`}
                        </button>
                        <button
                          className="decision-btn neutral"
                          onClick={() => setSelectedIssues(new Set(issues.map((_, i) => i)))}
                        >
                          Select all
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            ))
          }

          {phase === "busy" && (
            <div className="message-row ai animate-message">
              <div className="message-bubble">
                <div className="message-sender">KubeCheck</div>
                <div className="busy-line">
                  <div className="status-dot running" />
                  <span>{busyText}</span>
                </div>
                <div className="busy-meta">
                  <span>
                    {elapsed}s elapsed{etaText ? ` · ${etaText}` : ""}
                  </span>
                  <button className="cancel-btn" onClick={handleCancel}>
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>
        )}

        {hasStarted && <div className="chat-input-container">{composer}</div>}
      </main>
    </div>
  );
}
