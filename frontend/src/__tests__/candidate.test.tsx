import {act, fireEvent, screen, waitFor, within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {beforeEach, describe, expect, it, vi} from 'vitest';
import {api, ApiError} from '../lib/api';
import type {AuthSession, Job, SubmissionResult} from '../lib/types';
import {AppShell} from '../components/app-shell';
import {ApplicationDetailPage, ApplicationsPage, ApplyPage, JobDetailsPage, JobsPage} from '../pages/candidate';
import {candidateUser, engineeringJob, highCandidate, jobs, linkedinProfile, pendingCandidate, session} from './fixtures';
import {deferred, mockApi, renderPage} from './helpers';

beforeEach(mockApi);

describe('public opportunities', () => {
  it('lists API jobs and applies search, team, location, work mode, and contract filters together', async () => {
    const user = userEvent.setup();
    renderPage(<JobsPage />);
    expect(await screen.findByRole('link', {name: 'Frontend Engineer'})).toBeVisible();
    expect(screen.getByRole('link', {name: 'Product Designer'})).toBeVisible();
    await user.type(screen.getByRole('textbox', {name: 'Search jobs'}), 'Figma');
    expect(screen.queryByRole('link', {name: 'Frontend Engineer'})).not.toBeInTheDocument();
    await user.selectOptions(screen.getByRole('combobox', {name: 'department'}), 'Design');
    await user.selectOptions(screen.getByRole('combobox', {name: 'location'}), 'Remote');
    await user.selectOptions(screen.getByRole('combobox', {name: 'work mode'}), 'Remote');
    await user.selectOptions(screen.getByRole('combobox', {name: 'employment type'}), 'Contract');
    expect(screen.getByText('1 opportunity to explore')).toBeVisible();
    await user.selectOptions(screen.getByRole('combobox', {name: 'department'}), 'Engineering');
    expect(screen.getByText('No matches just yet')).toBeVisible();
    await user.click(screen.getAllByRole('button', {name: 'Clear filters'})[0]);
    expect(screen.getByRole('link', {name: 'Frontend Engineer'})).toBeVisible();
    expect(screen.getByRole('link', {name: 'Product Designer'})).toBeVisible();
  });

  it('initializes the listing from URL search and handles an empty source', async () => {
    renderPage(<JobsPage />, {path: '/jobs?search=TypeScript'});
    expect(await screen.findByRole('link', {name: 'Frontend Engineer'})).toBeVisible();
    expect(screen.getByRole('textbox', {name: 'Search jobs'})).toHaveValue('TypeScript');
    expect(screen.queryByRole('link', {name: 'Product Designer'})).not.toBeInTheDocument();
  });

  it('shows a meaningful empty state when there are no open jobs', async () => {
    vi.mocked(api.jobs).mockResolvedValue([]);
    renderPage(<JobsPage />);
    expect(await screen.findByText('Your next chapter is coming')).toBeVisible();
    expect(screen.queryByRole('article')).not.toBeInTheDocument();
  });

  it('shows loading and recovers from a job-list failure using the retry control', async () => {
    const request = deferred<Job[]>();
    vi.mocked(api.jobs).mockReturnValueOnce(request.promise).mockResolvedValueOnce(jobs);
    renderPage(<JobsPage />);
    expect(screen.getByRole('status', {name: 'Loading'})).toBeVisible();
    await act(async () => request.reject(new Error('Job service unavailable')));
    expect(await screen.findByRole('alert')).toHaveTextContent('Job service unavailable');
    await userEvent.click(screen.getByRole('button', {name: 'Try again'}));
    expect(await screen.findByRole('link', {name: 'Frontend Engineer'})).toBeVisible();
    expect(api.jobs).toHaveBeenCalledTimes(2);
  });

  it('renders the selected job details and carries the requirement into the apply link', async () => {
    renderPage(<JobDetailsPage />, {path: `/jobs/${engineeringJob.requirement_id}`, route: '/jobs/:requirementId'});
    expect(await screen.findByRole('heading', {name: engineeringJob.title, level: 1})).toBeVisible();
    expect(api.job).toHaveBeenCalledWith(engineeringJob.requirement_id);
    expect(screen.getByText(engineeringJob.description)).toBeVisible();
    expect(screen.getByText('React')).toBeVisible();
    expect(screen.getByText('Accessibility')).toBeVisible();
    expect(screen.getByRole('link', {name: 'Apply now'})).toHaveAttribute('href', `/jobs/${engineeringJob.requirement_id}/apply`);
  });

  it('sends an anonymous candidate directly through LinkedIn back to the selected role application', async () => {
    vi.mocked(api.session).mockResolvedValue(session(null));
    renderPage(<JobDetailsPage />, {path: '/jobs/' + engineeringJob.requirement_id, route: '/jobs/:requirementId'});
    const link = await screen.findByRole('link', {name: 'Sign in with LinkedIn to apply'});
    expect(link).toHaveAttribute('href', '/auth/linkedin/login?return_to=' + encodeURIComponent('/jobs/' + engineeringJob.requirement_id + '/apply'));
    expect(screen.getByText('Return to this role’s application after sign-in.')).toBeVisible();
  });
});

function renderApply() {
  return renderPage(<ApplyPage />, {path: `/jobs/${engineeringJob.requirement_id}/apply`, route: '/jobs/:requirementId/apply'});
}
async function readyForm() {
  await screen.findByRole('heading', {name: 'Make your next move.'});
  // Auth and job requests are independent; wait until profile prefilling finishes.
  await waitFor(() => expect(screen.getByRole('textbox', {name: 'Full name'})).toHaveValue('Alex Sample'));
}
async function attachResume() {
  await userEvent.upload(screen.getByLabelText('Upload resume'), new File(['%PDF-test resume'], 'sample.pdf', {type: 'application/pdf'}));
}

describe('candidate application submission', () => {
  it('displays the standard LinkedIn profile separately from editable application contacts', async () => {
    vi.mocked(api.session).mockResolvedValue(session({...candidateUser, linkedin_profile: linkedinProfile}));
    renderApply(); await readyForm();
    const profile = screen.getByRole('region', {name: 'Connected with LinkedIn'});
    expect(within(profile).getByText('Alex Sample')).toBeVisible();
    expect(within(profile).getByText('Alex')).toBeVisible();
    expect(within(profile).getByText('Sample')).toBeVisible();
    expect(within(profile).getByText('alex@example.test')).toBeVisible();
    expect(within(profile).getByText('Verified by LinkedIn')).toBeVisible();
    expect(within(profile).getByText('en-US')).toBeVisible();
    expect(within(profile).getByText(linkedinProfile.sub)).toBeVisible();
    expect(within(profile).queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.getByRole('img', {name: 'LinkedIn profile photo'})).toHaveAttribute('src', linkedinProfile.picture);
    expect(screen.getByRole('img', {name: 'LinkedIn profile photo'})).toHaveAttribute('referrerpolicy', 'no-referrer');
    await userEvent.clear(screen.getByRole('textbox', {name: 'Email address'}));
    await userEvent.type(screen.getByRole('textbox', {name: 'Email address'}), 'contact@example.test');
    expect(within(profile).getByText('alex@example.test')).toBeVisible();
    expect(screen.getByRole('textbox', {name: 'Email address'})).toHaveValue('contact@example.test');
  });

  it('keeps a legacy LinkedIn account URN out of the contact email field', async () => {
    vi.mocked(api.session).mockResolvedValue(session({
      ...candidateUser, email: linkedinProfile.sub,
      linkedin_profile: {...linkedinProfile, email: null, email_verified: null},
    }));
    renderApply(); await readyForm();
    expect(screen.getByRole('textbox', {name: 'Email address'})).toHaveValue('');
    expect(screen.getByText(linkedinProfile.sub)).toBeVisible();
    expect(screen.getByText(/LinkedIn did not provide a contact email/)).toBeVisible();
    expect(screen.getByRole('textbox', {name: /LinkedIn profile/})).toHaveValue('');
  });

  it('shows a compact connected account when LinkedIn shares only its identifier', async () => {
    vi.mocked(api.session).mockResolvedValue(session({...candidateUser, name: '', email: '', linkedin_profile: {sub: 'linked-account-only', issuer: linkedinProfile.issuer}}));
    renderApply();
    await screen.findByRole('heading', {name: 'Make your next move.'});
    const profile = screen.getByRole('region', {name: 'Connected with LinkedIn'});
    expect(within(profile).getByText('linked-account-only')).toBeVisible();
    expect(within(profile).queryByText('Full name')).not.toBeInTheDocument();
    expect(within(profile).queryByRole('img')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', {name: 'Full name'})).toHaveValue('');
    expect(screen.getByRole('textbox', {name: 'Email address'})).toHaveValue('');
  });

  it.each([
    'http://media.licdn.com/photo',
    'javascript:alert(1)',
    'https://media.licdn.com@external.example/photo',
    'https://media.licdn.com:8443/photo',
    'https://external.example/photo',
  ])('does not render an untrusted profile photo URL: %s', async (picture) => {
    vi.mocked(api.session).mockResolvedValue(session({...candidateUser, linkedin_profile: {...linkedinProfile, picture}}));
    renderApply(); await readyForm();
    expect(screen.queryByRole('img', {name: 'LinkedIn profile photo'})).not.toBeInTheDocument();
  });

  it('prefills valid profile contacts when flat session contact fields are missing', async () => {
    vi.mocked(api.session).mockResolvedValue(session({...candidateUser, name: '', email: '', linkedin_profile: {...linkedinProfile, name: null}}));
    renderApply(); await readyForm();
    expect(screen.getByRole('textbox', {name: 'Email address'})).toHaveValue('alex@example.test');
  });

  it('prefills editable application contact details and blocks submission without informed consent', async () => {
    renderApply(); await readyForm(); await attachResume();
    expect(screen.getByRole('textbox', {name: 'Full name'})).not.toHaveAttribute('readonly');
    expect(screen.getByRole('textbox', {name: 'Email address'})).not.toHaveAttribute('readonly');
    expect(screen.getByRole('textbox', {name: 'Email address'})).toHaveValue('alex@example.test');
    await userEvent.click(screen.getByRole('button', {name: 'Submit application'}));
    expect(await screen.findByRole('alert')).toHaveTextContent('Please accept the privacy consent to continue.');
    expect(api.submit).not.toHaveBeenCalled();
  });

  it('lets a signed-in candidate provide missing profile information and submits trimmed application contact details', async () => {
    vi.mocked(api.session).mockResolvedValue(session({...candidateUser, name: '', email: ''}));
    renderApply();
    await screen.findByRole('heading', {name: 'Make your next move.'});
    expect(screen.getByRole('textbox', {name: 'Full name'})).toHaveValue('');
    expect(screen.getByRole('textbox', {name: 'Email address'})).toHaveValue('');
    await userEvent.type(screen.getByRole('textbox', {name: 'Full name'}), '  Jamie Applicant  ');
    await userEvent.type(screen.getByRole('textbox', {name: 'Email address'}), '  jamie@example.test  ');
    await attachResume();
    await userEvent.click(screen.getByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', {name: 'Submit application'}));
    expect(await screen.findByTestId('application-success')).toBeVisible();
    const body = vi.mocked(api.submit).mock.calls[0][0];
    expect(body.get('candidate_name')).toBe('Jamie Applicant');
    expect(body.get('email')).toBe('jamie@example.test');
  });

  it('requires a full name and contact email when LinkedIn omits them', async () => {
    vi.mocked(api.session).mockResolvedValue(session({...candidateUser, name: '', email: ''}));
    renderApply();
    await screen.findByRole('heading', {name: 'Make your next move.'});
    await userEvent.type(screen.getByRole('textbox', {name: 'Full name'}), '   ');
    await attachResume();
    await userEvent.click(screen.getByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', {name: 'Submit application'}));
    expect(await screen.findByText('Enter your full name.')).toBeVisible();
    expect(screen.getByText('Enter your contact email.')).toBeVisible();
    expect(api.submit).not.toHaveBeenCalled();
  });

  it('validates an edited contact email before transmitting the application', async () => {
    renderApply(); await readyForm(); await attachResume();
    await userEvent.clear(screen.getByRole('textbox', {name: 'Email address'}));
    await userEvent.type(screen.getByRole('textbox', {name: 'Email address'}), 'invalid-email');
    await userEvent.click(screen.getByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', {name: 'Submit application'}));
    expect(await screen.findByText('Enter a valid email address.')).toBeVisible();
    expect(api.submit).not.toHaveBeenCalled();
  });

  it('prefills untouched contact fields when the session arrives after the job', async () => {
    const request = deferred<AuthSession>();
    vi.mocked(api.session).mockReturnValueOnce(request.promise);
    renderApply();
    await screen.findByRole('heading', {name: 'Make your next move.'});
    expect(screen.getByRole('textbox', {name: 'Full name'})).toHaveValue('');
    expect(screen.getByRole('textbox', {name: 'Email address'})).toHaveValue('');
    await act(async () => request.resolve(session()));
    await waitFor(() => expect(screen.getByRole('textbox', {name: 'Full name'})).toHaveValue('Alex Sample'));
    expect(screen.getByRole('textbox', {name: 'Email address'})).toHaveValue('alex@example.test');
  });

  it('preserves edited contact details when the session arrives later', async () => {
    const request = deferred<AuthSession>();
    vi.mocked(api.session).mockReturnValueOnce(request.promise);
    renderApply();
    await screen.findByRole('heading', {name: 'Make your next move.'});
    await userEvent.type(screen.getByRole('textbox', {name: 'Full name'}), 'Jamie Applicant');
    await userEvent.type(screen.getByRole('textbox', {name: 'Email address'}), 'jamie@example.test');
    await act(async () => request.resolve(session()));
    await screen.findByText(/Consent version v1/);
    expect(screen.getByRole('textbox', {name: 'Full name'})).toHaveValue('Jamie Applicant');
    expect(screen.getByRole('textbox', {name: 'Email address'})).toHaveValue('jamie@example.test');
  });

  it('does not refill a contact field that was typed into and then cleared while awaiting the session', async () => {
    const request = deferred<AuthSession>();
    vi.mocked(api.session).mockReturnValueOnce(request.promise);
    renderApply();
    await screen.findByRole('heading', {name: 'Make your next move.'});
    await userEvent.type(screen.getByRole('textbox', {name: 'Full name'}), 'Jamie Applicant');
    await userEvent.clear(screen.getByRole('textbox', {name: 'Full name'}));
    await act(async () => request.resolve(session()));
    await waitFor(() => expect(screen.getByRole('textbox', {name: 'Email address'})).toHaveValue('alex@example.test'));
    expect(screen.getByRole('textbox', {name: 'Full name'})).toHaveValue('');
  });

  it('requires a resume even after consent is accepted', async () => {
    renderApply(); await readyForm();
    await userEvent.click(screen.getByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', {name: 'Submit application'}));
    expect(await screen.findByRole('alert')).toHaveTextContent('Please attach your resume.');
    expect(api.submit).not.toHaveBeenCalled();
  });

  it.each([
    ['unsupported file type', new File(['text'], 'resume.txt', {type: 'text/plain'}), 'Please upload a PDF or DOCX document.'],
    ['empty document', new File([], 'resume.pdf', {type: 'application/pdf'}), 'Your resume file is empty. Please choose another document.'],
    ['document over 10 MB', new File([new Uint8Array(10 * 1024 * 1024 + 1)], 'resume.pdf', {type: 'application/pdf'}), 'Your resume must be 10 MB or smaller.'],
  ])('rejects an %s before any network submission', async (_, file, message) => {
    renderApply(); await readyForm();
    fireEvent.change(screen.getByLabelText('Upload resume'), {target: {files: [file]}});
    expect(screen.getByRole('alert')).toHaveTextContent(message);
    await userEvent.click(screen.getByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', {name: 'Submit application'}));
    expect(api.submit).not.toHaveBeenCalled();
  });

  it('validates LinkedIn profile URLs before transmitting the application', async () => {
    renderApply(); await readyForm(); await attachResume();
    await userEvent.type(screen.getByRole('textbox', {name: /LinkedIn profile/}), 'https://malicious.example/profile');
    await userEvent.click(screen.getByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', {name: 'Submit application'}));
    expect(await screen.findByText(/Enter an HTTPS LinkedIn profile URL/)).toBeVisible();
    expect(api.submit).not.toHaveBeenCalled();
  });

  it('sends multipart consent and selected requirement, disables repeated submits, and confirms the returned reference', async () => {
    const request = deferred<SubmissionResult>();
    vi.mocked(api.submit).mockReturnValueOnce(request.promise);
    renderApply(); await readyForm(); await attachResume();
    await userEvent.type(screen.getByRole('textbox', {name: /Phone number/}), '+91 9876543210');
    await userEvent.click(screen.getByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', {name: 'Submit application'}));
    await waitFor(() => expect(api.submit).toHaveBeenCalledTimes(1));
    const [body, idempotencyKey] = vi.mocked(api.submit).mock.calls[0];
    expect(body.get('requirement_id')).toBe(engineeringJob.requirement_id);
    expect(body.get('candidate_name')).toBe('Alex Sample');
    expect(body.get('email')).toBe('alex@example.test');
    expect(body.get('resume')).toBeInstanceOf(File);
    expect(body.get('phone')).toBe('+91 9876543210');
    expect(body.get('consent_accepted')).toBe('true');
    expect(body.get('consent_version')).toBe('v1');
    expect(idempotencyKey).toMatch(/^[\da-f]{8}-[\da-f-]{27}$/i);
    expect(screen.getByTestId('submit-application')).toBeDisabled();
    expect(screen.getByRole('textbox', {name: 'Full name'})).toBeDisabled();
    expect(screen.getByRole('textbox', {name: 'Email address'})).toBeDisabled();
    expect(screen.getByLabelText('Upload resume')).toBeDisabled();
    await act(async () => request.resolve({success: true, application_id: 'TEST-CONFIRMED-007', ai_status: 'queued', message: 'Application safely received.'}));
    expect(await screen.findByTestId('application-success')).toHaveTextContent('Application safely received.');
    expect(screen.getByTestId('application-id')).toHaveTextContent('TEST-CONFIRMED-007');
    expect(screen.getByRole('link', {name: 'Follow your application'})).toHaveAttribute('href', '/candidate/applications/TEST-CONFIRMED-007');
    expect(screen.getByText(/does not score your uploaded resume/)).toBeVisible();
  });

  it('preserves the uploaded document and idempotency key when retrying a failed submission', async () => {
    vi.mocked(api.submit).mockRejectedValueOnce(new Error('Document storage temporarily unavailable'));
    renderApply(); await readyForm(); await attachResume();
    await userEvent.click(screen.getByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', {name: 'Submit application'}));
    expect(await screen.findByRole('alert')).toHaveTextContent('Document storage temporarily unavailable');
    expect(screen.getByText('sample.pdf')).toBeVisible();
    expect(screen.getByTestId('submit-application')).toBeEnabled();
    expect(screen.getByRole('textbox', {name: 'Full name'})).toBeDisabled();
    expect(screen.getByRole('textbox', {name: 'Email address'})).toBeDisabled();
    expect(screen.getByRole('textbox', {name: /Phone number/})).toBeDisabled();
    expect(screen.getByRole('textbox', {name: /LinkedIn profile/})).toBeDisabled();
    expect(screen.getByLabelText('Upload resume')).toBeDisabled();
    expect(screen.getByRole('checkbox')).toBeDisabled();
    expect(screen.getByRole('button', {name: 'Remove selected resume'})).toBeDisabled();
    expect(screen.getByText(/Your original details and resume are kept/)).toBeVisible();
    await userEvent.click(screen.getByRole('button', {name: 'Retry original application'}));
    expect(await screen.findByTestId('application-success')).toBeVisible();
    expect(api.submit).toHaveBeenCalledTimes(2);
    expect(vi.mocked(api.submit).mock.calls[1][1]).toBe(vi.mocked(api.submit).mock.calls[0][1]);
    expect(vi.mocked(api.submit).mock.calls[1][0]).toBe(vi.mocked(api.submit).mock.calls[0][0]);
  });

  it('allows correcting a request rejected before reservation without reusing stale form data', async () => {
    vi.mocked(api.submit).mockRejectedValueOnce(new ApiError('Enter a valid contact email', 422));
    renderApply(); await readyForm(); await attachResume();
    await userEvent.click(screen.getByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', {name: 'Submit application'}));
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter a valid contact email');
    expect(screen.getByRole('textbox', {name: 'Email address'})).toBeEnabled();
    expect(screen.getByLabelText('Upload resume')).toBeEnabled();
    await userEvent.clear(screen.getByRole('textbox', {name: 'Email address'}));
    await userEvent.type(screen.getByRole('textbox', {name: 'Email address'}), 'corrected@example.test');
    await userEvent.click(screen.getByRole('button', {name: 'Submit application'}));
    expect(await screen.findByTestId('application-success')).toBeVisible();
    const [original, retry] = vi.mocked(api.submit).mock.calls;
    expect(retry[0]).not.toBe(original[0]);
    expect(retry[0].get('email')).toBe('corrected@example.test');
    expect(retry[1]).toBe(original[1]);
  });

  it('keeps a failed ERP attempt intact and shows its sanitized support activity', async () => {
    vi.mocked(api.submit).mockRejectedValueOnce(new ApiError('D365 submit_application failed (HTTP 500).', 502, {
      issue_code: 'd365_unclassified', hint: 'Inspect the F&O exception using this activity.',
      activity_id: 'cb035259-4dd8-0000-ec66-bed5d84ddd01',
    }));
    renderApply(); await readyForm(); await attachResume();
    await userEvent.type(screen.getByRole('textbox', {name: /Phone number/}), '+91 9876543210');
    await userEvent.click(screen.getByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', {name: 'Submit application'}));
    expect(await screen.findByRole('alert')).toHaveTextContent('cb035259-4dd8-0000-ec66-bed5d84ddd01');
    expect(screen.getByRole('alert')).not.toHaveTextContent('Inspect the F&O exception');
    fireEvent.change(screen.getByRole('textbox', {name: /Phone number/}), {target: {value: '12345'}});
    fireEvent.change(screen.getByLabelText('Upload resume'), {target: {files: [new File(['changed'], 'changed.pdf', {type: 'application/pdf'})]}});
    await userEvent.click(screen.getByRole('button', {name: 'Retry original application'}));
    expect(await screen.findByTestId('application-success')).toBeVisible();
    const [original, retry] = vi.mocked(api.submit).mock.calls;
    expect(retry[1]).toBe(original[1]);
    expect(retry[0].get('phone')).toBe('+91 9876543210');
    expect((retry[0].get('resume') as File).name).toBe('sample.pdf');
  });

  it('shows and submits the consent version supplied by the authenticated session', async () => {
    vi.mocked(api.session).mockResolvedValue({...session(), consent_version: 'test-consent-v2'});
    renderApply(); await readyForm(); await attachResume();
    expect(screen.getByText(/Consent version test-consent-v2/)).toBeVisible();
    await userEvent.click(screen.getByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', {name: 'Submit application'}));
    await screen.findByTestId('application-success');
    expect(vi.mocked(api.submit).mock.calls[0][0].get('consent_version')).toBe('test-consent-v2');
  });
});

describe('candidate status visibility', () => {
  it('uses a complete greeting when the signed-in candidate has no profile name', async () => {
    vi.mocked(api.session).mockResolvedValue(session({...candidateUser, name: '', email: ''}));
    renderPage(<ApplicationsPage />);
    await screen.findByRole('row', {name: /Frontend Engineer/});
    expect(screen.getByRole('heading', {name: 'Your story, in motion.'})).toBeVisible();
  });

  it('displays a generic candidate label in the header when the profile name is missing', async () => {
    vi.mocked(api.session).mockResolvedValue(session({...candidateUser, name: '', email: ''}));
    const {container} = renderPage(<AppShell />);
    await waitFor(() => expect(container.querySelector('.user-chip strong')).toHaveTextContent('Candidate'));
    expect(container.querySelector('.user-chip .avatar')).toHaveTextContent('C');
  });

  it('links application records without exposing internal AI processing', async () => {
    vi.mocked(api.applications).mockResolvedValue([pendingCandidate]);
    renderPage(<ApplicationsPage />);
    const row = await screen.findByRole('row', {name: /Frontend Engineer/});
    expect(within(row).getByText('Submitted')).toBeVisible();
    expect(within(row).queryByText('Queued')).not.toBeInTheDocument();
    expect(screen.queryByText('AI processing')).not.toBeInTheDocument();
    expect(within(row).getByRole('link', {name: 'Frontend Engineer'})).toHaveAttribute('href', `/candidate/applications/${pendingCandidate.application_id}`);
    expect(screen.queryByText(/86\/100/)).not.toBeInTheDocument();
  });

  it('shows candidate-owned details without exposing internal evaluation evidence', async () => {
    renderPage(<ApplicationDetailPage />, {path: `/candidate/applications/${highCandidate.application_id}`, route: '/candidate/applications/:applicationId'});
    expect(await screen.findByText(highCandidate.resume_file_name)).toBeVisible();
    expect(api.application).toHaveBeenCalledWith(highCandidate.application_id);
    expect(screen.queryByText(highCandidate.evaluation!.score_justification)).not.toBeInTheDocument();
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
    expect(screen.getByText(/The recruitment team will review your details and resume/)).toBeVisible();
  });

  it('keeps failed internal processing private while confirming the candidate application', async () => {
    const internalProcessingRecord = {...highCandidate, ai_status: 'Failed', evaluation: null};
    vi.mocked(api.application).mockResolvedValue(internalProcessingRecord);
    renderPage(<ApplicationDetailPage />, {path: `/candidate/applications/${highCandidate.application_id}`, route: '/candidate/applications/:applicationId'});
    expect(await screen.findByText('Your application is received')).toBeVisible();
    expect(screen.getByText('Submitted')).toBeVisible();
    expect(screen.queryByText('Failed')).not.toBeInTheDocument();
    expect(screen.queryByText('AI processing')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', {name: 'Retry evaluation'})).not.toBeInTheDocument();
  });
});
