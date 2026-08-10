// Single source of truth for the firm's checklist. The prompt is generated from it and
// Parse validates the model's answer against it, so prompt and validation cannot drift apart.
// Keys are exactly the values hvl_cases_case_type_check accepts.
const CATALOGUE = {
  'Family Visa': [
    'Petition I-130','Proof of US Petitioner Status','Proof of Relationship','Beneficiary passport',
    'I-864 Affidavit of Support','DS-260 form','Medical exam I-693','Police clearance certificates',
    'Birth certificates for dependents'],
  'Employment-Based Green Card': [
    'I-140 approval notice','Priority Date confirmation','I-485 application',
    'Employment verification letter','Educational credentials','Professional certifications',
    'I-864 Affidavit of Support','Medical exam I-693','Tax returns last 3 years',
    'Passport copies all pages'],
  'Asylum': [
    'I-589 application','Personal statement detailed','Country condition evidence',
    'Police/medical/human rights reports','Identity documents','Evidence of persecution',
    'Witness statements if available','Photos of harm','Attorney G-28 form',
    'Translations of non-English documents'],
  'Naturalization': [
    'I-751 if applicable','Green card copy both sides','Passport photos x2',
    'Tax returns last 5 years','N-400 application','Travel history documentation',
    'Selective Service proof','Criminal history disclosure','Employment history last 5 years'],
  'Other': []
};

const fields   = $('Set Fields').first().json;
const caseType = fields.case_type;
const docDesc  = fields.document_description;
const checklist = CATALOGUE[caseType] || [];

const catalogueText = Object.keys(CATALOGUE)
  .filter(k => CATALOGUE[k].length > 0)
  .map(k => k.toUpperCase() + ': ' + CATALOGUE[k].join(', '))
  .join('\n');

const prompt = `You are a paralegal assistant for an immigration law firm. Analyze document completeness for immigration cases.
CASE TYPE: ${caseType}
CLIENT DESCRIPTION:
${docDesc}
Required documents by case type:
${catalogueText}
Use the document names exactly as written in the list above. Do not invent, rename, merge or qualify them.
LANGUAGE
Return TWO language fields, and do not conflate them.
"detected_language": the language the client actually wrote CLIENT DESCRIPTION in, named in English. If it is neither Spanish nor English, say so plainly - for example "Japanese", "Portuguese", "Haitian Creole". Never substitute a language the firm supports for the one you actually read.
"preferred_language": the language the firm will reply in. Exactly "Spanish" or "English". If detected_language is neither, return "English".
Respond ONLY with valid JSON, no markdown, no preamble:
{
  "completeness_score": <integer from 0 to 100, a whole number. 100 means every required document is present. Never a fraction, never a ratio, never a decimal>,
  "completeness_label": "Complete|Mostly Complete|Incomplete|Critically Incomplete",
  "documents_present": [],
  "documents_missing": [],
  "documents_optional": [],
  "ai_summary": "2-3 sentence summary",
  "ai_notes": "flags or empty string",
  "priority_flag": false,
  "confidence_level": "High|Medium|Low",
  "detected_language": "<the language the client actually wrote in, in English>",
  "preferred_language": "Spanish|English"
}`;

return [{ json: { deepseek_body: prompt, checklist: checklist, case_type: caseType } }];
