import { useEffect, useRef, useState, type DragEvent, type FormEvent } from "react";
import {
  Link,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import {
  ArrowDown,
  ArrowRight,
  ArrowUpRight,
  BriefcaseBusiness,
  CalendarDays,
  Check,
  CheckCircle2,
  ChevronLeft,
  Clock3,
  FileCheck2,
  FileText,
  Fingerprint,
  Globe2,
  Linkedin,
  ListFilter,
  MapPin,
  Search,
  ShieldCheck,
  Sparkles,
  UploadCloud,
  Users,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { api, ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import { returnPath, signInHref } from "../lib/sign-in";
import { contactEmail } from "../lib/profile";
import { LinkedInProfileSummary } from "../components/linkedin-profile";
import {
  dateLabel,
  emptyFilters,
  experience,
  filterJobs,
  type JobFilters,
} from "../lib/utils";
import {
  Avatar,
  Badge,
  Breadcrumbs,
  CheckList,
  EmptyState,
  ErrorState,
  JobCard,
  JobIcon,
  Loading,
  PageHeading,
  SectionHeading,
  Spinner,
  StatusBadge,
} from "../components/common";
import { Button } from "../components/ui/button";
import { RecordScroll } from "../components/record-scroll";

function useJobs() {
  return useQuery({ queryKey: ["jobs"], queryFn: () => api.jobs() });
}

export function HomePage() {
  const jobs = useJobs();
  const { session } = useAuth();
  const departments = new Set(
    jobs.data?.map((job) => job.department).filter(Boolean),
  );
  return (
    <div className="page home-page">
      <div className="welcome-line">
        <div>
          <span className="eyebrow">CAREERS AT UBTI</span>
          <h1>Good people. Meaningful work.</h1>
        </div>
        <span className="welcome-note">
          <Globe2 size={15} /> Technology. People. Possibility.
        </span>
      </div>
      <div className="home-top-grid">
        <section className="hero candidate-hero">
          <div className="hero-copy">
            <span className="hero-kicker">
              <span />
              YOUR NEXT CHAPTER STARTS HERE
            </span>
            <h2>
              Build what’s next.
              <br />
              Grow with <span>UBTI.</span>
            </h2>
            <p>
              Bring your ideas to a team shaping business through technology.
              Discover a role where your experience can make a difference.
            </p>
            <Button variant="dark" asChild>
              <Link to="/jobs">
                Explore open roles
                <span className="button-arrow">
                  <ArrowRight size={14} />
                </span>
              </Link>
            </Button>
            <div className="hero-footnote">
              <ShieldCheck size={13} />
              Real opportunities. People-first decisions.
            </div>
          </div>
          <div className="hero-illustration" aria-hidden="true">
            <div className="orbit orbit-one" />
            <div className="orbit orbit-two" />
            <div className="hero-spark spark-one">✦</div>
            <div className="hero-spark spark-two">✦</div>
            <div className="floating-talent talent-one">
              <Avatar name="Your potential" color={1} />
              <div>
                <span>YOUR POTENTIAL</span>
                <strong>Endless possibilities</strong>
              </div>
              <Check size={15} />
            </div>
            <div className="match-orb">
              <Sparkles size={46} strokeWidth={1.3} />
              <span>A thoughtful match</span>
            </div>
            <div className="floating-talent talent-two">
              <span className="talent-job-icon">
                <BriefcaseBusiness size={20} />
              </span>
              <div>
                <span>YOUR NEXT CHAPTER</span>
                <strong>Work that matters</strong>
              </div>
              <ArrowUpRight size={16} />
            </div>
          </div>
        </section>
        <section className="card opportunity-card">
          <span className="eyebrow">A SIMPLE START</span>
          <h2>Your next move,<br />one step at a time.</h2>
          <ol className="application-steps">
            <li>
              <span>01</span>
              <div>
                <strong>Find your role</strong>
                <p>Explore the work and the skills you can bring.</p>
              </div>
            </li>
            <li>
              <span>02</span>
              <div>
                <strong>Introduce yourself</strong>
                <p>Sign in with LinkedIn and confirm your details.</p>
              </div>
            </li>
            <li>
              <span>03</span>
              <div>
                <strong>Share your experience</strong>
                <p>Upload your resume for our team to review.</p>
              </div>
            </li>
          </ol>
          <Button variant="secondary" asChild>
            <Link
              to={
                session?.user?.role === "candidate"
                  ? "/candidate/applications"
                  : "/login"
              }
            >
              <Linkedin size={15} />
              {session?.user?.role === "candidate"
                ? "My applications"
                : "Continue with LinkedIn"}
              <ArrowRight size={13} />
            </Link>
          </Button>
        </section>
      </div>
      <div className="discovery-stats">
        <div>
          <span className="stat-icon purple-icon">
            <BriefcaseBusiness size={20} />
          </span>
          <div>
            <strong>{jobs.data ? jobs.data.length : "—"}</strong>
            <span>Open opportunities</span>
          </div>
          <ArrowUpRight size={17} />
        </div>
        <div>
          <span className="stat-icon pink-icon">
            <Users size={20} />
          </span>
          <div>
            <strong>{jobs.data ? departments.size : "—"}</strong>
            <span>Teams to explore</span>
          </div>
          <ArrowUpRight size={17} />
        </div>
        <div>
          <span className="stat-icon blue-icon">
            <Fingerprint size={20} />
          </span>
          <div>
            <strong>Your unique story</strong>
            <span>Evidence beyond keywords</span>
          </div>
          <Sparkles size={17} />
        </div>
      </div>
      <SectionHeading
        title="Find where you belong"
        description="Fresh opportunities. Your next great chapter."
        link="/jobs"
        linkText="Explore all roles"
      />
      {jobs.isPending ? (
        <Loading />
      ) : jobs.error ? (
        <ErrorState
          error={jobs.error}
          retry={() => {
            void jobs.refetch();
          }}
        />
      ) : jobs.data?.length ? (
        <div className="job-grid featured-grid">
          {jobs.data.slice(0, 3).map((job, index) => (
            <JobCard key={job.requirement_id} job={job} index={index} />
          ))}
        </div>
      ) : (
        <EmptyState
          title="New opportunities are on their way"
          description="There are no open roles right now. Check back soon for your next chapter."
        />
      )}
      <section className="how-it-works">
        <SectionHeading
          title="Good things start simply"
          description="Three small steps toward your next opportunity."
        />
        <div className="steps-grid">
          <div>
            <span className="step-number">01</span>
            <span className="step-line" />
            <h3>Find your possibility</h3>
            <p>
              Explore roles and discover a team where your experience can make a
              difference.
            </p>
          </div>
          <div>
            <span className="step-number">02</span>
            <span className="step-line" />
            <h3>Bring your story</h3>
            <p>
              Sign in with LinkedIn and share your resume. Your experience
              deserves its full context.
            </p>
          </div>
          <div>
            <span className="step-number">03</span>
            <h3>Let’s meet your potential</h3>
            <p>
              Our recruitment team reviews your application with evidence, care,
              and human judgment.
            </p>
          </div>
        </div>
      </section>
      {session?.demo_mode && (
        <div className="demo-strip">
          <Sparkles size={16} />
          <span>
            You’re exploring a demo. Opportunities and sample evaluations are
            fictional.
          </span>
        </div>
      )}
    </div>
  );
}

export function JobsPage() {
  const query = useJobs();
  const [searchParams] = useSearchParams();
  const [filters, setFilters] = useState<JobFilters>({
    ...emptyFilters,
    search: searchParams.get("search") || "",
  });
  const [visible, setVisible] = useState(9);
  const search = searchParams.get("search") || "";
  const [lastSearch, setLastSearch] = useState(search);
  if (search !== lastSearch) {
    setLastSearch(search);
    setFilters((value) => ({ ...value, search }));
    setVisible(9);
  }
  const jobs = filterJobs(query.data || [], filters);
  const active = Object.values(filters).some(Boolean);
  function change(key: keyof JobFilters, value: string) {
    setFilters((previous) => ({ ...previous, [key]: value }));
    setVisible(9);
  }
  return (
    <div className="page">
      <PageHeading
        eyebrow="FIND YOUR NEXT CHAPTER"
        title="Opportunity looks good on you."
        description="Explore open roles and find a place for your potential."
        action={
          <Badge tone="purple">
            <BriefcaseBusiness size={13} />
            {query.data?.length ?? "—"} open roles
          </Badge>
        }
      />
      <div className="job-list-toolbar card">
        <div className="filter-search">
          <Search size={18} />
          <input
            aria-label="Search jobs"
            placeholder="Job title, team, or skill…"
            value={filters.search}
            onChange={(event) => change("search", event.target.value)}
          />
        </div>
        <div className="filter-row">
          <span className="filter-caption">
            <ListFilter size={15} />
            Filter by
          </span>
          {(
            ["department", "location", "work_mode", "employment_type"] as const
          ).map((key) => (
            <select
              aria-label={key.replaceAll("_", " ")}
              key={key}
              value={filters[key]}
              onChange={(event) => change(key, event.target.value)}
            >
              <option value="">
                {key === "department"
                  ? "All teams"
                  : key === "location"
                    ? "All locations"
                    : key === "work_mode"
                      ? "Work mode"
                      : "Job type"}
              </option>
              {Array.from(
                new Set(query.data?.map((job) => job[key]).filter(Boolean)),
              ).map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          ))}
          {active && (
            <button
              className="clear-filter"
              onClick={() => {
                setFilters(emptyFilters);
                setVisible(9);
              }}
            >
              Clear filters
              <X size={13} />
            </button>
          )}
        </div>
      </div>
      <div className="results-heading">
        <span>
          {query.isPending
            ? "Finding opportunities…"
            : `${jobs.length} ${jobs.length === 1 ? "opportunity" : "opportunities"} to explore`}
        </span>
        <span>
          <Sparkles size={13} />
          Make your next move meaningful
        </span>
      </div>
      {query.isPending ? (
        <Loading />
      ) : query.error ? (
        <ErrorState
          error={query.error}
          retry={() => {
            void query.refetch();
          }}
        />
      ) : jobs.length ? (
        <RecordScroll label="Open roles" layout="cards" className="job-results" resetKey={JSON.stringify(filters)}>
          <div className="job-grid">
            {jobs.slice(0, visible).map((job, index) => (
              <JobCard key={job.requirement_id} job={job} index={index} />
            ))}
          </div>
          {jobs.length > visible && (
            <div className="load-more">
              <Button
                variant="outline"
                onClick={() => setVisible((value) => value + 9)}
              >
                Explore more roles
                <ArrowDown size={15} />
              </Button>
            </div>
          )}
        </RecordScroll>
      ) : (
        <EmptyState
          title={active ? "No matches just yet" : "Your next chapter is coming"}
          description={
            active
              ? "Try a different search or loosen your filters to discover more opportunities."
              : "There are no open roles at the moment. Please check back soon."
          }
          action={
            active && (
              <Button
                variant="secondary"
                onClick={() => setFilters(emptyFilters)}
              >
                Clear filters
              </Button>
            )
          }
        />
      )}
    </div>
  );
}

export function JobDetailsPage() {
  const { requirementId = "" } = useParams();
  const query = useQuery({
    queryKey: ["job", requirementId],
    queryFn: () => api.job(requirementId),
  });
  const { session } = useAuth();
  if (query.isPending)
    return (
      <div className="page">
        <Loading />
      </div>
    );
  if (query.error)
    return (
      <div className="page">
        <ErrorState
          error={query.error}
          retry={() => {
            void query.refetch();
          }}
        />
      </div>
    );
  const job = query.data;
  return (
    <div className="page">
      <Breadcrumbs
        items={[{ label: "Opportunities", to: "/jobs" }, { label: job.title }]}
      />
      <div className="job-detail-header">
        <JobIcon department={job.department} />
        <div>
          {job.department && <span className="eyebrow">{job.department}</span>}
          <h1>{job.title}</h1>
          <div className="job-detail-meta">
            {job.location && (
              <span>
                <MapPin size={15} />
                {job.location}
              </span>
            )}
            {job.work_mode && (
              <span>
                <Globe2 size={15} />
                {job.work_mode}
              </span>
            )}
            {job.employment_type && (
              <span>
                <Clock3 size={15} />
                {job.employment_type}
              </span>
            )}
          </div>
        </div>
        <StatusBadge status={job.status} />
      </div>
      <div className="details-grid">
        <article className="card job-description" aria-label="Job description" tabIndex={0}>
          <h2>A role with room for your potential.</h2>
          {job.description ? (
            <p className="preserve-lines">{job.description}</p>
          ) : (
            <p>
              The recruitment team has not provided a description for this role.
            </p>
          )}
          {job.responsibilities && (
            <>
              <h3>What you’ll do</h3>
              <p className="preserve-lines">{job.responsibilities}</p>
            </>
          )}
          {job.required_skills.length > 0 && (
            <>
              <h3>What you bring</h3>
              <CheckList items={job.required_skills} />
            </>
          )}
          {job.preferred_skills.length > 0 && (
            <>
              <h3>A little extra to make you shine</h3>
              <div className="skills-list">
                {job.preferred_skills.map((skill) => (
                  <Badge key={skill} tone="purple">
                    {skill}
                  </Badge>
                ))}
              </div>
            </>
          )}
          {job.qualifications && (
            <>
              <h3>Qualifications</h3>
              <p className="preserve-lines">{job.qualifications}</p>
            </>
          )}
          <div className="inclusive-note">
            <Users size={18} />
            <p>
              Your experience has a story. If this role speaks to your
              strengths, we’d love to hear it.
            </p>
          </div>
        </article>
        <aside>
          <section className="card role-summary">
            <h3>The opportunity, at a glance</h3>
            <dl>
              {job.department && (
                <div>
                  <dt>Team</dt>
                  <dd>{job.department}</dd>
                </div>
              )}
              {job.location && (
                <div>
                  <dt>Location</dt>
                  <dd>{job.location}</dd>
                </div>
              )}
              {job.work_mode && (
                <div>
                  <dt>Work mode</dt>
                  <dd>{job.work_mode}</dd>
                </div>
              )}
              {job.employment_type && (
                <div>
                  <dt>Commitment</dt>
                  <dd>{job.employment_type}</dd>
                </div>
              )}
              {experience(job) && (
                <div>
                  <dt>Experience</dt>
                  <dd>{experience(job)}</dd>
                </div>
              )}
              {job.openings != null && (
                <div>
                  <dt>Openings</dt>
                  <dd>{job.openings}</dd>
                </div>
              )}
              {job.published_date && (
                <div>
                  <dt>Published</dt>
                  <dd>{dateLabel(job.published_date)}</dd>
                </div>
              )}
              {job.deadline && (
                <div>
                  <dt>Apply before</dt>
                  <dd>{dateLabel(job.deadline)}</dd>
                </div>
              )}
              <div>
                <dt>Requirement ID</dt>
                <dd className="mono">{job.requirement_id}</dd>
              </div>
            </dl>
            <Button asChild className="full-width">
              {session?.user?.role === "candidate" ? (
                <Link to={`/jobs/${encodeURIComponent(job.requirement_id)}/apply`}>
                  Apply now
                  <ArrowRight size={16} />
                </Link>
              ) : (
                <a href={signInHref(false, `/jobs/${encodeURIComponent(job.requirement_id)}/apply`)}>
                  <Linkedin size={16} />
                  Sign in with LinkedIn to apply
                </a>
              )}
            </Button>
            {!session?.user && (
              <p className="small-note">
                <Linkedin size={13} />
                Return to this role’s application after sign-in.
              </p>
            )}
          </section>
          <div className="application-aside-note">
            <ShieldCheck size={22} />
            <h4>Your story stays yours.</h4>
            <p>
              Your application is shared with the recruitment team. AI supports
              their review; people make the decisions.
            </p>
          </div>
        </aside>
      </div>
    </div>
  );
}

export function LoginPage({ hr = false }: { hr?: boolean }) {
  const { session, loginDemo, loginHR, loading, error, refresh } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [busy, setBusy] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loginError, setLoginError] = useState("");
  const requestedReturn = returnPath(location.state?.from, hr);
  const from = requestedReturn || (hr ? "/hr" : "/candidate/applications");
  async function signInHR(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || loading || error) return;
    setBusy(true);
    setLoginError("");
    try {
      await loginHR(username, password);
      setPassword("");
      navigate(from, { replace: true });
    } catch (value) {
      setLoginError(value instanceof Error ? value.message : "Unable to sign in.");
    } finally {
      setBusy(false);
    }
  }
  async function demo() {
    setBusy(true);
    try {
      await loginDemo(hr ? "hr" : "candidate");
      toast.success(
        `Welcome to the ${hr ? "HR" : "candidate"} demo workspace.`,
      );
      navigate(from, { replace: true });
    } catch (value) {
      toast.error(
        value instanceof Error ? value.message : "Unable to sign in.",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="page login-page">
      <div className="login-art">
        <span className="hero-kicker">A THOUGHTFUL MATCH</span>
        <h1>
          {hr ? (
            <>
              Great teams
              <br />
              start with <em>people.</em>
            </>
          ) : (
            <>
              Your next chapter
              <br />
              starts with <em>you.</em>
            </>
          )}
        </h1>
        <p>
          {hr
            ? "One connected workspace for opportunities, candidate evidence, and the people behind every resume."
            : "Let your experience speak. Find meaningful opportunities, share your story, and make your next move."}
        </p>
        <div className="login-visual" aria-hidden="true">
          <span>✦</span>
          <div className="login-orbit" />
          <div className="login-person">
            <Avatar name={hr ? "Recruitment team" : "Your story"} size="lg" />
            <strong>
              {hr
                ? "Human judgment. Better context."
                : "Your potential belongs here."}
            </strong>
            <Badge>
              <Sparkles size={12} />
              {hr
                ? "Evidence, thoughtfully reviewed"
                : "The beginning of something great"}
            </Badge>
          </div>
        </div>
      </div>
      <div className="card login-card">
        <span className="login-icon">
          {hr ? <ShieldCheck size={27} /> : <Linkedin size={27} />}
        </span>
        <span className="eyebrow">
          {hr ? "RECRUITMENT WORKSPACE" : "CANDIDATE PORTAL"}
        </span>
        <h2>{hr ? "Hello, team." : "Hello, possibility."}</h2>
        <p>
          {hr
            ? "Enter your HR username and password to open your recruitment workspace."
            : "Sign in with LinkedIn to apply for roles and follow your applications."}
        </p>
        {session?.user?.role === (hr ? "hr" : "candidate") && (
          <Button asChild className="full-width">
            <Link to={from}>
              Continue to your workspace
              <ArrowRight size={16} />
            </Link>
          </Button>
        )}
        {hr ? (
          <form className="hr-login-form" onSubmit={(event) => { void signInHR(event); }}>
            <label className="form-field">
              <span>Username</span>
              <input name="username" type="text" autoComplete="username" maxLength={320} required
                value={username} onChange={(event) => setUsername(event.target.value)} disabled={busy} />
            </label>
            <label className="form-field">
              <span>Password</span>
              <input name="password" type="password" autoComplete="current-password" maxLength={1024} required
                value={password} onChange={(event) => setPassword(event.target.value)} disabled={busy} />
            </label>
            {loginError && <p role="alert" className="field-error">{loginError}</p>}
            <Button type="submit" variant="dark" className="full-width" disabled={busy || loading || !!error}>
              {busy ? <Spinner label="Signing in…" /> : <>Sign in to HR <ArrowRight size={15} /></>}
            </Button>
          </form>
        ) : (
          <Button asChild variant="dark" className="full-width">
            <a href={signInHref(false, requestedReturn)}>
              <Linkedin size={17} />Continue with LinkedIn<ArrowRight size={15} />
            </a>
          </Button>
        )}
        <div className="login-security">
          <ShieldCheck size={13} />
          {hr ? "Your credentials are checked securely by the server." : "Secure sign-in. No passwords to remember."}
        </div>
        {loading ? (
          <Spinner label="Checking workspace…" />
        ) : error ? (
          <ErrorState error={error} retry={refresh} />
        ) : (
          !hr && session?.demo_mode && (
            <div className="login-demo">
              <div className="divider-label">
                <span />
                OR EXPLORE THE DEMO
                <span />
              </div>
              <Badge tone="orange">Development mode</Badge>
              <p>
                A working preview with fictional jobs and candidate evidence. No
                external sign-in needed.
              </p>
              <Button
                data-testid={hr ? "demo-hr-login" : "demo-candidate-login"}
                variant="secondary"
                className="full-width"
                onClick={() => {
                  void demo();
                }}
                disabled={busy}
              >
                {busy ? (
                  <Spinner label="Opening workspace…" />
                ) : (
                  <>
                    <Sparkles size={16} />
                    {hr ? "Explore HR demo" : "Explore candidate demo"}
                    <ArrowRight size={15} />
                  </>
                )}
              </Button>
            </div>
          )
        )}
        <div className="login-footer-link">
          {hr ? "Looking for your next opportunity?" : "Find a role that fits your experience."}
          <Link to={hr ? "/login" : "/jobs"}>
            {hr ? "Candidate sign-in" : "Explore open roles"}
            <ArrowUpRight size={12} />
          </Link>
        </div>
      </div>
    </div>
  );
}

const applicationSchema = z.object({
  candidate_name: z
    .string()
    .trim()
    .min(1, "Enter your full name.")
    .max(200, "Use a full name of 200 characters or fewer."),
  email: z
    .string()
    .trim()
    .min(1, "Enter your contact email.")
    .max(320, "Use an email address of 320 characters or fewer.")
    .email("Enter a valid email address."),
  phone: z
    .string()
    .trim()
    .max(40, "Use a phone number of 40 characters or fewer."),
  linkedin_url: z
    .string()
    .trim()
    .refine(
      (value) =>
        !value ||
        /^https:\/\/(www\.)?linkedin\.com\/in\/[a-zA-Z0-9_%.-]+\/?(?:\?.*)?$/.test(
          value,
        ),
      "Enter an HTTPS LinkedIn profile URL, such as https://www.linkedin.com/in/your-name.",
    ),
  consent: z
    .boolean()
    .refine((value) => value, "Please accept the privacy consent to continue."),
});
type ApplicationFields = z.infer<typeof applicationSchema>;
// eslint-disable-next-line react-refresh/only-export-components
export function validateResume(file: File | null) {
  if (!file) return "Please attach your resume.";
  if (!/\.(pdf|docx)$/i.test(file.name))
    return "Please upload a PDF or DOCX document.";
  if (file.size > 10 * 1024 * 1024)
    return "Your resume must be 10 MB or smaller.";
  if (file.size === 0)
    return "Your resume file is empty. Please choose another document.";
  return null;
}

export function ApplyPage() {
  const client = useQueryClient();
  const { requirementId = "" } = useParams();
  const query = useQuery({
    queryKey: ["job", requirementId],
    queryFn: () => api.job(requirementId),
  });
  const { session } = useAuth();
  const user = session?.user;
  const profileName = user?.name.trim() || user?.linkedin_profile?.name?.trim() ||
    [user?.linkedin_profile?.given_name, user?.linkedin_profile?.family_name].filter(Boolean).join(" ").trim();
  const profileEmail = contactEmail(user?.email) || contactEmail(user?.linkedin_profile?.email);
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitDiagnostics, setSubmitDiagnostics] = useState<ApiError["diagnostics"]>();
  const [dragging, setDragging] = useState(false);
  const [result, setResult] = useState<{
    application_id: string;
    message: string;
  } | null>(null);
  const [submissionKey] = useState(() => crypto.randomUUID());
  const submittedForm = useRef<FormData | null>(null);
  const [submissionFrozen, setSubmissionFrozen] = useState(false);
  const contactEdited = useRef({ candidate_name: false, email: false });
  const {
    register,
    handleSubmit,
    getFieldState,
    setValue,
    formState: { errors, isSubmitting },
  } = useForm<ApplicationFields>({
    resolver: zodResolver(applicationSchema),
    defaultValues: {
      candidate_name: profileName,
      email: profileEmail,
      phone: "",
      linkedin_url: "",
      consent: false,
    },
  });
  useEffect(() => {
    if (submissionFrozen) return;
    const profile = { candidate_name: profileName, email: profileEmail };
    for (const key of ["candidate_name", "email"] as const) {
      const state = getFieldState(key);
      if (
        profile[key] &&
        !contactEdited.current[key] &&
        !state.isDirty &&
        !state.isTouched
      ) {
        setValue(key, profile[key]);
      }
    }
  }, [profileName, profileEmail, getFieldState, setValue, submissionFrozen]);
  const nameField = register("candidate_name");
  const emailField = register("email");
  function selectFile(value: File | null) {
    if (submissionFrozen || isSubmitting) return;
    setFileError(validateResume(value));
    setFile(value);
  }
  function drop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragging(false);
    if (!isSubmitting && !submissionFrozen) selectFile(event.dataTransfer.files[0] || null);
  }
  async function submit(values: ApplicationFields) {
    const error = validateResume(file);
    setFileError(error);
    if (error || !file) return;
    setSubmitError(null);
    setSubmitDiagnostics(undefined);
    // A lost response or ERP 5xx can leave a durable pending application.
    // Retry that exact multipart payload/key instead of rebuilding from edits.
    const data = submittedForm.current || new FormData();
    if (!submittedForm.current) {
      data.set("requirement_id", requirementId);
      data.set("candidate_name", values.candidate_name);
      data.set("email", values.email);
      data.set("resume", file);
      data.set("phone", values.phone);
      data.set("linkedin_url", values.linkedin_url);
      data.set("consent_accepted", "true");
      data.set("consent_version", session?.consent_version || "v1");
      submittedForm.current = data;
    }
    setSubmissionFrozen(true);
    try {
      const response = await api.submit(data, submissionKey);
      setResult(response);
      void client.invalidateQueries({ queryKey: ["applications"] });
      toast.success("Your application is on its way.");
    } catch (value) {
      setSubmitDiagnostics(value instanceof ApiError ? value.diagnostics : undefined);
      // These local request/auth/validation responses occur before reservation.
      // ERP failures are surfaced as 5xx and must retain the frozen attempt.
      if (value instanceof ApiError && [400, 401, 403, 413, 415, 422, 429].includes(value.status)) {
        submittedForm.current = null;
        setSubmissionFrozen(false);
      }
      setSubmitError(
        value instanceof Error
          ? value.message
          : "We couldn’t submit your application. Please try again.",
      );
    }
  }
  if (query.isPending)
    return (
      <div className="page">
        <Loading />
      </div>
    );
  if (query.error)
    return (
      <div className="page">
        <ErrorState
          error={query.error}
          retry={() => {
            void query.refetch();
          }}
        />
      </div>
    );
  const job = query.data;
  if (result)
    return (
      <div className="page">
        <div
          className="card application-success"
          data-testid="application-success"
        >
          <span className="success-icon">
            <CheckCircle2 size={42} />
          </span>
          <Badge tone="green">APPLICATION RECEIVED</Badge>
          <h1>Your next chapter is in motion.</h1>
          <p>{result.message}</p>
          <div className="confirmation-details">
            <span>You applied for</span>
            <strong>{job.title}</strong>
            <span>Application reference</span>
            <code data-testid="application-id">{result.application_id}</code>
          </div>
          {session?.demo_mode && (
            <p className="demo-copy">
              This demo submission is stored locally. Any sample evaluation is
              fictional and does not score your uploaded resume.
            </p>
          )}
          <Button asChild>
            <Link to={`/candidate/applications/${result.application_id}`}>
              Follow your application
              <ArrowRight size={15} />
            </Link>
          </Button>
          <Link to="/jobs" className="text-link">
            Keep exploring
            <ArrowUpRight size={14} />
          </Link>
        </div>
      </div>
    );
  return (
    <div className="page">
      <Breadcrumbs
        items={[
          { label: "Opportunities", to: "/jobs" },
          { label: job.title, to: `/jobs/${requirementId}` },
          { label: "Your application" },
        ]}
      />
      <PageHeading
        eyebrow="BRING YOUR STORY"
        title="Make your next move."
        description={`You’re applying for ${job.title}. Let’s introduce you.`}
      />
      <div className="details-grid application-grid">
        <form
          className="card application-form"
          onSubmit={(event) => { void handleSubmit(submit)(event); }}
          noValidate
        >
          {user?.linkedin_profile && (
            <LinkedInProfileSummary profile={user.linkedin_profile} />
          )}
          <div className="form-section-title">
            <span>01</span>
            <div>
              <h2>A little about you</h2>
              <p>Confirm your name and contact email for this application.</p>
            </div>
          </div>
          <div className="form-grid">
            <div className="form-field">
              <label htmlFor="candidate-name">Full name</label>
              <input
                id="candidate-name"
                autoComplete="name"
                disabled={isSubmitting || submissionFrozen}
                {...nameField}
                onChange={(event) => {
                  contactEdited.current.candidate_name = true;
                  void nameField.onChange(event);
                }}
                aria-invalid={Boolean(errors.candidate_name)}
              />
              {errors.candidate_name && (
                <span className="field-error">
                  {errors.candidate_name.message}
                </span>
              )}
            </div>
            <div className="form-field">
              <label htmlFor="candidate-email">Email address</label>
              <input
                id="candidate-email"
                type="email"
                autoComplete="email"
                disabled={isSubmitting || submissionFrozen}
                {...emailField}
                onChange={(event) => {
                  contactEdited.current.email = true;
                  void emailField.onChange(event);
                }}
                aria-invalid={Boolean(errors.email)}
              />
              {errors.email && (
                <span className="field-error">{errors.email.message}</span>
              )}
            </div>
            <div className="form-field">
              <label htmlFor="phone">
                Phone number <span>(optional)</span>
              </label>
              <input
                id="phone"
                type="tel"
                disabled={isSubmitting || submissionFrozen}
                autoComplete="tel"
                placeholder="Your contact number"
                {...register("phone")}
                aria-invalid={Boolean(errors.phone)}
              />
              {errors.phone && (
                <span className="field-error">{errors.phone.message}</span>
              )}
            </div>
            <div className="form-field">
              <label htmlFor="linkedin-url">
                LinkedIn profile <span>(optional)</span>
              </label>
              <input
                id="linkedin-url"
                type="url"
                disabled={isSubmitting || submissionFrozen}
                placeholder="https://linkedin.com/in/your-name"
                {...register("linkedin_url")}
                aria-invalid={Boolean(errors.linkedin_url)}
              />
              {errors.linkedin_url && (
                <span className="field-error">
                  {errors.linkedin_url.message}
                </span>
              )}
            </div>
          </div>
          <div className="form-section-title">
            <span>02</span>
            <div>
              <h2>Let your experience speak</h2>
              <p>Share a resume that tells your story.</p>
            </div>
          </div>
          <label
            htmlFor="resume-upload"
            className={`resume-dropzone ${dragging ? "dropzone-active" : ""} ${fileError ? "dropzone-error" : ""}`}
            onDragOver={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={drop}
          >
            <span className="upload-icon">
              <UploadCloud size={25} />
            </span>
            <strong>
              {file
                ? "Your story is ready to share"
                : "Drop your resume right here"}
            </strong>
            <span>
              or <u>browse your files</u>
            </span>
            <small>PDF or DOCX · Up to 10 MB</small>
            <input
              id="resume-upload"
              aria-label="Upload resume"
              data-testid="resume-input"
              name="resume"
              type="file"
              accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
              disabled={isSubmitting || submissionFrozen}
              onChange={(event) => selectFile(event.target.files?.[0] || null)}
            />
          </label>
          {file && (
            <div className="selected-file">
              <span className="file-type-icon">
                <FileText size={21} />
              </span>
              <div>
                <strong>{file.name}</strong>
                <span>
                  {(file.size / 1024).toFixed(1)} KB · Ready to upload
                </span>
              </div>
              <button
                type="button"
                aria-label="Remove selected resume"
                disabled={isSubmitting || submissionFrozen}
                onClick={() => {
                  setFile(null);
                  setFileError(null);
                }}
              >
                <X size={17} />
              </button>
            </div>
          )}
          {fileError && (
            <p className="field-error" role="alert">
              {fileError}
            </p>
          )}
          <div className="consent-box">
            <label className="checkbox-label">
              <input
                data-testid="application-consent"
                type="checkbox"
                disabled={isSubmitting || submissionFrozen}
                {...register("consent")}
                aria-invalid={Boolean(errors.consent)}
              />
              <span>
                I consent to processing my application and resume for
                recruitment, including AI-assisted evidence analysis. I
                understand that the recruitment team makes the hiring decisions.
              </span>
            </label>
            <p>
              Consent version {session?.consent_version || "v1"}. Your
              information is used to review your application for this role and
              manage the recruitment process.
            </p>
          </div>
          {errors.consent && (
            <p className="field-error" role="alert">
              {errors.consent.message}
            </p>
          )}
          {session?.demo_mode && (
            <div className="inline-demo-note">
              <Sparkles size={16} />
              <p>
                Demo mode: your document will be stored locally. Any evaluation
                shown is a labeled fictional sample, not an analysis of your
                resume.
              </p>
            </div>
          )}
          {submitError && (
            <div className="submit-error" role="alert">
              {submitError}
              {submitDiagnostics?.activity_id && (
                <p>Support activity: {submitDiagnostics.activity_id}</p>
              )}
            </div>
          )}
          {submitError && submissionFrozen && (
            <p role="status">
              Your original details and resume are kept for this attempt. Retry
              sends that same application. Please contact HR before starting a
              separate application or replacing these details while confirmation
              is pending.
            </p>
          )}
          <div className="form-actions">
            <Link to={`/jobs/${requirementId}`} className="text-link">
              <ChevronLeft size={14} />
              Back to the role
            </Link>
            <Button
              type="submit"
              data-testid="submit-application"
              disabled={isSubmitting}
            >
              {isSubmitting ? (
                <Spinner label="Submitting your application…" />
              ) : (
                <>
                  {submissionFrozen ? "Retry original application" : "Submit application"}
                  <ArrowRight size={16} />
                </>
              )}
            </Button>
          </div>
        </form>
        <aside>
          <section className="card application-job-card">
            <JobIcon department={job.department} />
            <Badge>{job.department || "Open opportunity"}</Badge>
            <h3>{job.title}</h3>
            <div className="application-job-meta">
              {job.location && (
                <span>
                  <MapPin size={14} />
                  {job.location}
                </span>
              )}
              {job.employment_type && (
                <span>
                  <Clock3 size={14} />
                  {job.employment_type}
                </span>
              )}
              {experience(job) && (
                <span>
                  <BriefcaseBusiness size={14} />
                  {experience(job)}
                </span>
              )}
            </div>
            <hr />
            <h4>What happens next?</h4>
            <ol className="next-steps">
              <li>
                <span>1</span>
                <div>
                  <strong>Application received</strong>
                  <p>We save your details and resume.</p>
                </div>
              </li>
              <li>
                <span>2</span>
                <div>
                  <strong>A thoughtful review</strong>
                  <p>
                    Our team considers your experience with evidence and
                    context.
                  </p>
                </div>
              </li>
              <li>
                <span>3</span>
                <div>
                  <strong>Stay in the loop</strong>
                  <p>Follow your application status in your workspace.</p>
                </div>
              </li>
            </ol>
          </section>
          <div className="application-aside-note">
            <ShieldCheck size={22} />
            <h4>Secure from the first hello.</h4>
            <p>
              Your resume is only available to authorized recruitment users.
            </p>
          </div>
        </aside>
      </div>
    </div>
  );
}

export function ApplicationsPage() {
  const { session } = useAuth();
  const firstName = session?.user?.name.trim().split(/\s+/)[0];
  const query = useQuery({
    queryKey: ["applications"],
    queryFn: api.applications,
    refetchInterval: 30_000,
  });
  return (
    <div className="page">
      <PageHeading
        eyebrow="YOUR CANDIDATE WORKSPACE"
        title={`Your story, in motion${firstName ? `, ${firstName}` : ""}.`}
        description="Every application is the beginning of a possibility. Follow yours here."
        action={
          <Button asChild variant="secondary">
            <Link to="/jobs">
              Explore more roles
              <ArrowUpRight size={15} />
            </Link>
          </Button>
        }
      />
      <div className="applications-banner">
        <FileCheck2 size={28} />
        <div>
          <h3>A thoughtful review takes a little time.</h3>
          <p>
            Your recruitment team reviews your experience with care. You can
            check the latest status below.
          </p>
        </div>
        <Sparkles size={35} strokeWidth={1.2} />
      </div>
      {query.isPending ? (
        <Loading />
      ) : query.error ? (
        <ErrorState
          error={query.error}
          retry={() => {
            void query.refetch();
          }}
        />
      ) : query.data?.length ? (
        <div className="card application-list">
          <RecordScroll label="My applications" className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Opportunity</th>
                  <th>Application reference</th>
                  <th>Applied</th>
                  <th>Application status</th>
                  <th>
                    <span className="sr-only">Details</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {query.data.map((item) => (
                  <tr key={item.application_id}>
                    <td>
                      <Link
                        className="table-job-title"
                        to={`/candidate/applications/${item.application_id}`}
                      >
                        {item.job_title}
                      </Link>
                      {item.demo_fixture && (
                        <Badge tone="orange">Demo sample</Badge>
                      )}
                    </td>
                    <td className="mono">{item.application_id}</td>
                    <td>{dateLabel(item.applied_date)}</td>
                    <td>
                      <StatusBadge status={item.hr_status} />
                    </td>
                    <td>
                      <Link
                        className="round-link"
                        aria-label={`View application for ${item.job_title}`}
                        to={`/candidate/applications/${item.application_id}`}
                      >
                        <ArrowUpRight size={15} />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </RecordScroll>
        </div>
      ) : (
        <EmptyState
          title="Your first possibility is waiting"
          description="You haven’t applied for a role yet. Explore open opportunities and find a place for your potential."
          icon={<FileCheck2 />}
          action={
            <Button asChild>
              <Link to="/jobs">
                Find your next opportunity
                <ArrowRight size={15} />
              </Link>
            </Button>
          }
        />
      )}
    </div>
  );
}

export function ApplicationDetailPage() {
  const { applicationId = "" } = useParams();
  const query = useQuery({
    queryKey: ["application", applicationId],
    queryFn: () => api.application(applicationId),
    refetchInterval: 30_000,
  });
  if (query.isPending)
    return (
      <div className="page">
        <Loading />
      </div>
    );
  if (query.error)
    return (
      <div className="page">
        <ErrorState
          error={query.error}
          retry={() => {
            void query.refetch();
          }}
        />
      </div>
    );
  const item = query.data;
  return (
    <div className="page">
      <Breadcrumbs
        items={[
          { label: "My applications", to: "/candidate/applications" },
          { label: item.job_title },
        ]}
      />
      <PageHeading
        eyebrow="YOUR APPLICATION"
        title={item.job_title}
        description="Your experience is in good hands. Here’s where things stand."
      />
      <div className="details-grid">
        <section className="card application-overview">
          <div className="application-overview-heading">
            <span className="success-icon small">
              <FileCheck2 size={26} />
            </span>
            <div>
              <h2>Your application is received</h2>
              <p>Thank you for bringing your story.</p>
            </div>
            {item.demo_fixture && <Badge tone="orange">Demo sample</Badge>}
          </div>
          <dl className="application-detail-list">
            <div>
              <dt>Application reference</dt>
              <dd className="mono">{item.application_id}</dd>
            </div>
            <div>
              <dt>Candidate</dt>
              <dd>{item.candidate_name}</dd>
            </div>
            <div>
              <dt>Email</dt>
              <dd>{item.email}</dd>
            </div>
            <div>
              <dt>Applied on</dt>
              <dd>{dateLabel(item.applied_date)}</dd>
            </div>
            <div>
              <dt>Resume</dt>
              <dd>
                <FileText size={15} />
                {item.resume_file_name || "Submitted resume"}
              </dd>
            </div>
            <div>
              <dt>Application status</dt>
              <dd>
                <StatusBadge status={item.hr_status} />
              </dd>
            </div>
          </dl>
          <div className="inclusive-note">
            <Clock3 size={19} />
            <p>
              Your application has been received. The recruitment team will
              review your details and resume. You can follow updates here.
            </p>
          </div>
        </section>
        <aside className="card application-job-card">
          <CalendarDays size={27} className="text-primary" />
          <h3>
            Every next chapter
            <br />
            starts somewhere.
          </h3>
          <p>
            Keep exploring while our team considers your application. Your
            potential can have more than one possibility.
          </p>
          <Button asChild variant="secondary">
            <Link to="/jobs">
              Explore more opportunities
              <ArrowRight size={15} />
            </Link>
          </Button>
        </aside>
      </div>
    </div>
  );
}
