SET lock_timeout = '2s';

CREATE OR REPLACE FUNCTION ats_jobs_search_raw(raw jsonb)
RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
    SELECT jsonb_build_object(
        'description', raw->'description', 'content', raw->'content',
        'descriptionHtml', raw->'descriptionHtml', 'descriptionPlain', raw->'descriptionPlain',
        'externalDescription', raw->'externalDescription', 'jobDescription', raw->'jobDescription',
        'job_description', raw->'job_description', 'Job_Description', raw->'Job_Description',
        'body', raw->'body', 'content_html', raw->'content_html',
        'description_html', raw->'description_html', 'descriptionBody', raw->'descriptionBody',
        'text', raw->'text', '_jobposting', raw->'_jobposting',
        'ShortDescriptionStr', raw->'ShortDescriptionStr', 'company', raw->'company',
        'hiringOrganization', raw->'hiringOrganization', 'employer', raw->'employer',
        'entreprise', raw->'entreprise', 'firma', raw->'firma',
        'company_name', raw->'company_name', 'Company_Name', raw->'Company_Name')
$$;
