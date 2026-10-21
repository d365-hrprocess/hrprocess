import type {
  Application,
  AuthSession,
  Candidate,
  CandidateResponse,
  Job,
  SubmissionResult,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public diagnostics?: {issue_code?: string; hint?: string; activity_id?: string},
  ) {
    super(message);
    this.name = "ApiError";
  }
}
let csrfToken = "";
export function setCsrfToken(token: string) {
  csrfToken = token;
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  if (options.method && !["GET", "HEAD"].includes(options.method.toUpperCase()))
    headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(path, {
    ...options,
    credentials: "include",
    headers,
  });
  if (!response.ok) {
    let message = `Request failed (${response.status}). Please try again.`;
    let diagnostics: ApiError["diagnostics"];
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
      if (body.diagnostics && typeof body.diagnostics === "object") {
        diagnostics = {};
        for (const key of ["issue_code", "hint", "activity_id"] as const) {
          if (typeof body.diagnostics[key] === "string") diagnostics[key] = body.diagnostics[key];
        }
      }
    } catch {
      /* A gateway can return HTML. */
    }
    throw new ApiError(message, response.status, diagnostics);
  }
  return response.json() as Promise<T>;
}

export const api = {
  session: async () => {
    const value = await request<AuthSession>("/api/auth/me");
    setCsrfToken(value.csrf_token);
    return value;
  },
  demoLogin: async (role: "candidate" | "hr") => {
    const value = await request<AuthSession>("/api/auth/demo", {
      method: "POST",
      body: JSON.stringify({ role }),
    });
    setCsrfToken(value.csrf_token);
    return value;
  },
  hrLogin: async (username: string, password: string) => {
    const value = await request<AuthSession>("/api/auth/hr", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    setCsrfToken(value.csrf_token);
    return value;
  },
  logout: () =>
    request<{ success: boolean }>("/api/auth/logout", { method: "POST" }),
  jobs: (hr = false) => request<Job[]>(hr ? "/api/hr/jobs" : "/api/jobs"),
  job: (id: string) => request<Job>(`/api/jobs/${encodeURIComponent(id)}`),
  applications: () => request<Application[]>("/api/me/applications"),
  application: (id: string) =>
    request<Application>(`/api/applications/${encodeURIComponent(id)}`),
  candidates: (id: string) =>
    request<CandidateResponse>(
      `/api/hr/jobs/${encodeURIComponent(id)}/candidates`,
    ),
  candidate: (jobId: string, id: string) =>
    request<Candidate>(
      `/api/hr/jobs/${encodeURIComponent(jobId)}/candidates/${encodeURIComponent(id)}`,
    ),
  retryEvaluation: (jobId: string, id: string) =>
    request<{ success: boolean; application_id: string; ai_status: string; message: string }>(
      `/api/hr/jobs/${encodeURIComponent(jobId)}/candidates/${encodeURIComponent(id)}/retry`,
      { method: "POST" },
    ),
  submit: (data: FormData, key: string) =>
    request<SubmissionResult>("/api/applications", {
      method: "POST",
      body: data,
      headers: { "Idempotency-Key": key },
    }),
};

export function resumeUrl(id: string, requirementId?: string) {
  return `/api/hr/applications/${encodeURIComponent(id)}/resume${requirementId ? `?requirement_id=${encodeURIComponent(requirementId)}` : ""}`;
}
