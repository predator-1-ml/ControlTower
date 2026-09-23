-- Demo fixtures. Re-runnable: truncates first, so `make seed` is always safe.
--
-- The first four customers and first three claims are the ones the tests and
-- the demo runbook name; their values are load-bearing and must not change.
-- The rest exist so the book looks like a book: every claim status and type,
-- every onboarding branch (including the age rejection, which no fixture
-- reached before), a customer with more than two open claims for the
-- escalation rule, and a policy corpus wide enough that a retrieval which
-- returns the wrong passage is a visible failure rather than an impossible one.
--
-- Nothing here is real. Names, references and amounts are invented.

TRUNCATE claims, applications, customers RESTART IDENTITY CASCADE;
TRUNCATE knowledge_chunks RESTART IDENTITY;

-- ---------------------------------------------------------------- customers
INSERT INTO customers (external_ref, full_name, email, date_of_birth, kyc_status) VALUES
    -- The assignment's worked example: "onboard this customer and check whether
    -- they already have an active claim." Verified, and does have one.
    ('CUST-1001', 'Priya Raman',    'priya.raman@example.com',    '1988-04-12', 'verified'),
    -- Unverified: onboarding pauses for documents, then approves. Beat 3.
    ('CUST-1002', 'Daniel Okafor',  'daniel.okafor@example.com',  '1995-11-03', 'unverified'),
    -- verify_identity fails -> manual_review branch, no pause.
    ('CUST-1003', 'Mei Lin',        'mei.lin@example.com',        '1979-02-27', 'failed'),
    -- Has a claim with gaps -> claims workflow pauses on request_information.
    ('CUST-1004', 'Tom Baker',      'tom.baker@example.com',      '1965-08-19', 'verified'),
    -- Clean path with no pause: verified, adult, nothing on file -> application
    -- created. The one to use when the demo needs onboarding to just succeed.
    ('CUST-1005', 'Aisha Rahman',   'aisha.rahman@example.com',   '1992-06-30', 'verified'),
    -- Seventeen: the reject branch (minimum age 18). Turns 18 on 2027-03-15,
    -- after which this row exercises the clean path instead — move the date if
    -- the fixture outlives that.
    ('CUST-1006', 'Luca Moretti',   'luca.moretti@example.com',   '2009-03-15', 'verified'),
    -- Pending, not unverified: routes the same way (request_documents) and
    -- proves the branch is "not verified", not "== unverified".
    ('CUST-1007', 'Grace Mwangi',   'grace.mwangi@example.com',   '1983-10-08', 'pending'),
    -- Three open claims: the runbook escalation rule for "more than two open
    -- claims", and a motor claim that triggers both amount rules at once.
    ('CUST-1008', 'Hiro Tanaka',    'hiro.tanaka@example.com',    '1970-01-22', 'verified'),
    -- Only closed claims. Onboarding must reach application_created: the active
    -- filter is what keeps a settled claim from forcing a manual review.
    ('CUST-1009', 'Sofia Alvarez',  'sofia.alvarez@example.com',  '1998-12-05', 'verified'),
    -- One claim under review, nothing missing: the summarise path with no pause.
    ('CUST-1010', 'Ben Carter',     'ben.carter@example.com',     '1990-07-14', 'verified'),
    -- A second unverified customer, so Beat 3 can be repeated without re-seeding.
    ('CUST-1011', 'Nadia Haddad',   'nadia.haddad@example.com',   '1987-02-19', 'unverified'),
    -- One missing field, not two: the pause asks for a single thing.
    ('CUST-1012', 'Samuel Osei',    'samuel.osei@example.com',    '1959-05-02', 'verified');

-- ------------------------------------------------------------------- claims
INSERT INTO claims (claim_ref, customer_id, claim_type, status, amount, incident_date, missing_fields)
SELECT v.claim_ref, c.id, v.claim_type, v.status, v.amount, v.incident_date, v.missing_fields
FROM (VALUES
    -- Active. This is the one the worked example must surface.
    ('CLM-5001', 'CUST-1001', 'motor',    'open',                  4820.00, DATE '2026-08-21', '[]'::jsonb),
    -- Settled, same customer: proves the "active" filter actually filters.
    ('CLM-5002', 'CUST-1001', 'travel',   'settled',                310.00, DATE '2025-12-02', '[]'::jsonb),
    -- Incomplete: drives request_information instead of generate_summary.
    ('CLM-5003', 'CUST-1004', 'property', 'awaiting_information', 12750.00, DATE '2026-09-01',
        '["incident_report", "police_reference"]'::jsonb),
    -- Over both motor thresholds: escalation AND second review, in one answer.
    ('CLM-5004', 'CUST-1008', 'motor',    'open',                 15200.00, DATE '2026-09-10', '[]'::jsonb),
    ('CLM-5005', 'CUST-1008', 'property', 'open',                  3100.00, DATE '2026-08-02', '[]'::jsonb),
    -- A travel claim waiting on receipts: the third open claim for CUST-1008.
    ('CLM-5006', 'CUST-1008', 'travel',   'awaiting_information',   890.00, DATE '2026-07-19',
        '["receipts"]'::jsonb),
    -- Closed history for CUST-1009: neither may count as active.
    ('CLM-5007', 'CUST-1009', 'motor',    'settled',               2150.00, DATE '2025-06-11', '[]'::jsonb),
    ('CLM-5008', 'CUST-1009', 'travel',   'rejected',               640.00, DATE '2025-11-30', '[]'::jsonb),
    -- Under review with everything on file: "Proceed with standard assessment".
    ('CLM-5009', 'CUST-1010', 'travel',   'under_review',          2400.00, DATE '2026-09-05', '[]'::jsonb),
    -- One outstanding field; property, so no second-review rule applies.
    ('CLM-5010', 'CUST-1012', 'property', 'awaiting_information',  7800.00, DATE '2026-08-28',
        '["police_reference"]'::jsonb),
    -- Old settled motor claim for Tom Baker: his active list stays CLM-5003 alone.
    ('CLM-5011', 'CUST-1004', 'motor',    'settled',               1200.00, DATE '2024-03-03', '[]'::jsonb)
) AS v(claim_ref, external_ref, claim_type, status, amount, incident_date, missing_fields)
JOIN customers c ON c.external_ref = v.external_ref;

-- ------------------------------------------------------------- applications
INSERT INTO applications (customer_id, product, status, decision_reason)
SELECT c.id, v.product, v.status, v.decision_reason
FROM (VALUES
    ('CUST-1001', 'motor_policy',    'approved',      NULL),
    ('CUST-1009', 'travel_policy',   'approved',      NULL),
    ('CUST-1008', 'property_policy', 'manual_review', 'existing active claims: CLM-5004, CLM-5005, CLM-5006'),
    ('CUST-1003', 'motor_policy',    'rejected',      'identity verification failed'),
    ('CUST-1010', 'motor_policy',    'submitted',     NULL)
) AS v(external_ref, product, status, decision_reason)
JOIN customers c ON c.external_ref = v.external_ref;

-- --------------------------------------------------------- knowledge_chunks
-- embedding is left NULL: it is populated by the ingestion step, which needs an
-- embedding model. Retrieval must therefore tolerate un-embedded rows rather
-- than assuming the column is always present.
--
-- The numbers in the first three sections and in "Registering a claim" are
-- quoted by the claims workflow (ESCALATION_LIMIT, SECOND_REVIEW_LIMIT,
-- REQUIRED_DOCUMENTS) and a test reads this file to check they still agree.
-- Every other section is knowledge only: nothing in code applies it, so nothing
-- in code can contradict it.
INSERT INTO knowledge_chunks (source, section, content) VALUES
    -- claims-handling-policy.md ------------------------------------------
    ('claims-handling-policy.md', 'Motor claims',
     'Motor claims under 5000 are settled by a single assessor. Claims of 5000 or '
     'more require a second review before settlement. An assessor must record an '
     'incident report reference for every motor claim.'),
    ('claims-handling-policy.md', 'Missing information',
     'When a claim is missing required information the handler moves it to '
     'awaiting_information and contacts the customer. A claim may sit in '
     'awaiting_information for 30 days before it is automatically closed.'),
    ('claims-handling-policy.md', 'Registering a claim',
     'A claim is registered against the customer it belongs to with the claim '
     'type (motor, property or travel), the amount claimed and the incident date. '
     'The claim type is required at registration; an amount or incident date not '
     'yet known may be added later. Motor and property claims are registered as '
     'awaiting information until an incident report reference is recorded; travel '
     'claims open immediately. A claim registered without a reference is assigned '
     'the next reference in sequence.'),
    ('claims-handling-policy.md', 'Property claims',
     'Property claims for theft or malicious damage require a police reference '
     'before assessment. Photographs of the damage are requested at registration '
     'but do not hold up assessment. Property claims are assessed by a single '
     'assessor regardless of amount; the escalation rules in the operations '
     'runbook still apply.'),
    ('claims-handling-policy.md', 'Travel claims',
     'Travel claims are supported by receipts for each expense claimed and, for a '
     'cancellation, confirmation from the carrier or provider. Travel claims are '
     'assessed by a single assessor regardless of amount.'),
    ('claims-handling-policy.md', 'Fraud indicators',
     'Refer a claim to the fraud team when the incident date is within 14 days of '
     'the policy start, when the same customer has reported three or more incidents '
     'in twelve months, or when the documents supplied do not match the details '
     'registered. A referral does not change the claim status; the fraud team '
     'records its finding on the claim.'),
    ('claims-handling-policy.md', 'Settlement',
     'A claim moves to under review once every required document is on file. '
     'Settlement is authorised by the assessor within their limit and by the duty '
     'manager above it. A rejected claim is notified to the customer in writing '
     'with the reason recorded on the claim.'),
    -- onboarding-policy.md -----------------------------------------------
    ('onboarding-policy.md', 'Identity verification',
     'Identity verification requires a government photo ID and proof of address '
     'issued within the last three months. A customer whose verification fails '
     'twice is routed to manual review and may not self-serve.'),
    ('onboarding-policy.md', 'Eligibility',
     'Applicants must be 18 or older and resident in a supported territory. An '
     'applicant with an unsettled claim on a previous policy is not automatically '
     'ineligible, but the application requires manual review.'),
    ('onboarding-policy.md', 'Accepted documents',
     'Accepted photo identification: a passport, a national identity card or a '
     'driving licence. Accepted proof of address: a bank statement, a utility bill '
     'or a council tax letter issued within the last three months. Documents must '
     'be legible; a photograph of a screen is not accepted.'),
    ('onboarding-policy.md', 'Manual review',
     'An application sent to manual review is picked up by the onboarding team '
     'within two working days. The reviewer records a decision reason on the '
     'application. A customer whose application is in manual review may not '
     'self-serve until the review is complete.'),
    -- operations-runbook.md ----------------------------------------------
    ('operations-runbook.md', 'Escalation',
     'Escalate to the duty manager when a claim exceeds 10000, when a customer '
     'has more than two open claims, or when identity verification fails for a '
     'customer who already holds an active policy.'),
    ('operations-runbook.md', 'Duty manager rota',
     'The duty manager is the senior handler on the rota for the day. On weekdays '
     'Farah Qureshi covers 08:00 to 14:00 and Ben Whitfield covers 14:00 to 20:00. '
     'Lena Fischer covers weekends and public holidays. Reach the duty manager on '
     'the ops-escalations channel; out of hours, use the on-call phone listed on '
     'the rota page.'),
    ('operations-runbook.md', 'Service levels',
     'Acknowledge a new claim within one working day of registration. Request '
     'missing information within two working days. A claim with every document on '
     'file is assessed within ten working days. An escalated claim is reviewed by '
     'the duty manager within one working day.'),
    ('operations-runbook.md', 'Contacting customers',
     'Customers are contacted by email from the claims mailbox, with a phone call '
     'for an escalated claim. Never share the details of one customer with another, '
     'and never confirm that a claim exists to anyone other than the policyholder '
     'or their named representative.'),
    ('operations-runbook.md', 'Data handling',
     'Customer references (CUST-) and claim references (CLM-) may be quoted in '
     'internal channels. Full names, dates of birth and addresses are not pasted '
     'into chat channels; refer to the record by its reference instead.');
