-- Demo fixtures. Re-runnable: truncates first, so `make seed` is always safe.
--
-- The four customers are not arbitrary — each one drives a different branch of
-- the onboarding and claims workflows, so the demo can show conditional routing
-- rather than just describing it.

TRUNCATE claims, applications, customers RESTART IDENTITY CASCADE;
TRUNCATE knowledge_chunks RESTART IDENTITY;

-- ---------------------------------------------------------------- customers
INSERT INTO customers (external_ref, full_name, email, date_of_birth, kyc_status) VALUES
    -- The assignment's worked example: "onboard this customer and check whether
    -- they already have an active claim." Verified, and does have one.
    ('CUST-1001', 'Priya Raman',   'priya.raman@example.com',   '1988-04-12', 'verified'),
    -- Clean path: nothing on file, onboarding runs start to finish.
    ('CUST-1002', 'Daniel Okafor', 'daniel.okafor@example.com', '1995-11-03', 'unverified'),
    -- verify_identity fails -> manual_review branch.
    ('CUST-1003', 'Mei Lin',       'mei.lin@example.com',       '1979-02-27', 'failed'),
    -- Has a claim with gaps -> claims workflow pauses on request_information.
    ('CUST-1004', 'Tom Baker',     'tom.baker@example.com',     '1965-08-19', 'verified');

-- ------------------------------------------------------------------- claims
INSERT INTO claims (claim_ref, customer_id, claim_type, status, amount, incident_date, missing_fields)
SELECT v.claim_ref, c.id, v.claim_type, v.status, v.amount, v.incident_date, v.missing_fields
FROM (VALUES
    -- Active. This is the one the worked example must surface.
    ('CLM-5001', 'CUST-1001', 'motor',    'open',                  4820.00, DATE '2026-08-21', '[]'::jsonb),
    -- Settled, same customer — proves the "active" filter actually filters.
    ('CLM-5002', 'CUST-1001', 'travel',   'settled',                310.00, DATE '2025-12-02', '[]'::jsonb),
    -- Incomplete: drives request_information instead of generate_summary.
    ('CLM-5003', 'CUST-1004', 'property', 'awaiting_information', 12750.00, DATE '2026-09-01',
        '["incident_report", "police_reference"]'::jsonb)
) AS v(claim_ref, external_ref, claim_type, status, amount, incident_date, missing_fields)
JOIN customers c ON c.external_ref = v.external_ref;

-- ------------------------------------------------------------- applications
INSERT INTO applications (customer_id, product, status)
SELECT c.id, 'motor_policy', 'approved'
FROM customers c WHERE c.external_ref = 'CUST-1001';

-- --------------------------------------------------------- knowledge_chunks
-- embedding is left NULL: it is populated by the ingestion step, which needs an
-- embedding model. Retrieval must therefore tolerate un-embedded rows rather
-- than assuming the column is always present.
INSERT INTO knowledge_chunks (source, section, content) VALUES
    ('claims-handling-policy.md', 'Motor claims',
     'Motor claims under 5000 are settled by a single assessor. Claims of 5000 or '
     'more require a second review before settlement. An assessor must record an '
     'incident report reference for every motor claim.'),
    ('claims-handling-policy.md', 'Missing information',
     'When a claim is missing required information the handler moves it to '
     'awaiting_information and contacts the customer. A claim may sit in '
     'awaiting_information for 30 days before it is automatically closed.'),
    ('onboarding-policy.md', 'Identity verification',
     'Identity verification requires a government photo ID and proof of address '
     'issued within the last three months. A customer whose verification fails '
     'twice is routed to manual review and may not self-serve.'),
    ('onboarding-policy.md', 'Eligibility',
     'Applicants must be 18 or older and resident in a supported territory. An '
     'applicant with an unsettled claim on a previous policy is not automatically '
     'ineligible, but the application requires manual review.'),
    ('operations-runbook.md', 'Escalation',
     'Escalate to the duty manager when a claim exceeds 10000, when a customer '
     'has more than two open claims, or when identity verification fails for a '
     'customer who already holds an active policy.');
