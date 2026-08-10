// Deterministic boundary. Below this line the rules are ours, not the model's.
const ALLOWED_LABEL = ['Complete','Mostly Complete','Incomplete','Critically Incomplete'];
const ALLOWED_CONF  = ['High','Medium','Low'];
const ALLOWED_LANG  = ['Spanish','English'];

const raw  = $input.first().json;
const text = raw?.choices?.[0]?.message?.content ?? '';
const fields = $('Set Fields').first().json;
const violations = [];

// A form submitted with no description is not a client with no documents — it is a client
// who told us nothing. Scoring it 0 and mailing them the entire checklist is a verdict the
// system invented, the same error as marking a document present because the model inferred
// it. IF Has Description routes these past the model entirely; this flag is checked here as
// well so that a mis-routed run still refuses to invent an answer.
const nothingToReview = String(fields.document_description || '').trim().length === 0;

// The Deepseek API node continues on error into this node rather than throwing, because a
// run that throws here dies three nodes before Respond to Webhook and n8n closes the
// still-open request with a bare 200 — the caller is told nothing is wrong and the intake
// is lost. Measured 2026-08-09: a timed-out model call returned exactly that.
//
// A real completion always carries `choices`. Anything without it is the error output, and
// saying "not valid JSON" about it would be true and useless: the model never answered.
const callFailed = !raw || !Array.isArray(raw.choices);
const callError = callFailed
  ? String(raw?.error?.message || raw?.error || raw?.message || 'no response').slice(0, 300)
  : '';

let parsed = {};
let unparseable = false;
if (callFailed) {
  unparseable = true;
  // Only a violation when a call was actually attempted. The no-description branch also
  // arrives here without `choices`, and there the absence of a completion is the intended
  // outcome rather than a breach.
  if (!nothingToReview) violations.push('the model call did not return a completion: ' + callError);
} else {
  try {
    const clean = String(text).replace(/```json/gi, '').replace(/```/g, '').trim();
    const p = JSON.parse(clean);
    if (p && typeof p === 'object' && !Array.isArray(p)) { parsed = p; }
    else { unparseable = true; violations.push('model output was JSON but not an object'); }
  } catch (e) {
    unparseable = true;
    violations.push('model output was not valid JSON');
  }
}

const strArray = (v, field) => {
  if (v === undefined || v === null) return [];
  if (!Array.isArray(v)) { violations.push(field + ' was not an array'); return []; }
  return v.map(x => (typeof x === 'string' ? x : JSON.stringify(x))).filter(s => s.length > 0);
};

const oneOf = (v, allowed, field, fallback) => {
  if (typeof v === 'string' && allowed.includes(v)) return v;
  violations.push(field + ' was ' + JSON.stringify(v) + ', not one of ' + allowed.join(' / '));
  return fallback;
};

// completeness_score is an integer 0-100 in the database.
// A non-integer strictly between 0 and 1 is the right answer in the wrong unit, so scale it;
// anything else is clamped into range. Either way the coercion is recorded, never silent.
let scoreUnreadable = false;
const scoreOf = (v) => {
  const n = Number(v);
  if (v === undefined || v === null || v === '' || !Number.isFinite(n)) {
    violations.push('completeness_score was ' + JSON.stringify(v) + ', not a number');
    scoreUnreadable = true;
    return 0;
  }
  let s = n;
  if (!Number.isInteger(n) && n > 0 && n < 1) {
    s = n * 100;
    violations.push('completeness_score arrived as the ratio ' + n + ', read as ' + Math.round(s) + ' out of 100');
  }
  const r = Math.round(s);
  const clamped = Math.max(0, Math.min(100, r));
  if (clamped !== r) violations.push('completeness_score ' + n + ' was out of range, clamped to ' + clamped);
  return clamped;
};

const out = {
  completeness_score: scoreOf(parsed.completeness_score),
  completeness_label: oneOf(parsed.completeness_label, ALLOWED_LABEL, 'completeness_label', 'Unknown'),
  confidence_level:   oneOf(parsed.confidence_level,   ALLOWED_CONF,  'confidence_level',   'Low'),
  preferred_language: oneOf(parsed.preferred_language, ALLOWED_LANG,  'preferred_language', 'English'),
  documents_present:  strArray(parsed.documents_present,  'documents_present'),
  documents_missing:  strArray(parsed.documents_missing,  'documents_missing'),
  documents_optional: strArray(parsed.documents_optional, 'documents_optional'),
  ai_summary: typeof parsed.ai_summary === 'string' ? parsed.ai_summary : '',
  ai_notes:   typeof parsed.ai_notes   === 'string' ? parsed.ai_notes   : '',
  priority_flag: parsed.priority_flag === true
};

// Three states, not two. Downstream only needs to know whether anyone assessed these
// documents; the reason is for the paralegal, and the three read very differently to one.
// Not `callFailed`: when there was nothing to review the call was never made, and a flag
// that reports a failure which did not happen is the sort of small lie that becomes a
// wrong diagnosis at 2 a.m.
out.model_call_failed = callFailed && !nothingToReview;

// --- Language, and the difference between reading one and replying in one -----
// `preferred_language` is the reply language and is domain-constrained, so a client who
// writes Japanese comes back as "English" and nothing records that anything happened. The
// firm's rule is that it operates in Spanish and English only; that rule is fine, but a
// client is owed the knowledge that they are being answered in a language they did not use.
//
// Two independent signals, because neither alone is enough. The model is asked for
// `detected_language` separately from the reply language and can be wrong or silent; a
// non-Latin script in the client's own text is proof it did not write Spanish or English
// and needs no cooperation from anyone.
const NON_LATIN = /[\u0400-\u04FF\u0590-\u05FF\u0600-\u06FF\u0700-\u074F\u0900-\u097F\u0E00-\u0E7F\u1100-\u11FF\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF]/;
const declared = String(parsed.detected_language || '').trim();
const declaredNorm = declared.toLowerCase();
const scriptForeign = NON_LATIN.test(String(fields.document_description || ''));
const declaredForeign = declaredNorm.length > 0 &&
  declaredNorm.indexOf('spanish') === -1 && declaredNorm.indexOf('english') === -1;

out.detected_language = declared || null;
out.language_supported = !(scriptForeign || declaredForeign);
if (!out.language_supported && !nothingToReview) {
  out.priority_flag = true;
  out.ai_notes = (out.ai_notes ? out.ai_notes + ' | ' : '') +
    'Client wrote in ' + (declared || 'a non-Latin script') +
    '. The firm replies in Spanish and English only, so the confirmation went out in English and ' +
    'said so. This intake needs someone who can read it.';
}
out.nothing_to_review = nothingToReview;
out.review_ran = !callFailed && !nothingToReview;

if (nothingToReview) {
  // Deliberately NOT a contract violation. `contract_violated` means the model returned
  // something the contract forbids; here the model was never asked. An empty form is an
  // input condition, and calling it a violation would inflate the one rate the firm uses
  // to judge whether the classifier is behaving. Flagged for a human on its own merits.
  out.priority_flag = true;
  out.ai_summary = 'No documents were described, so there was nothing to review. The intake is recorded and the client has been asked to tell us what they have.';
  out.documents_missing = ['Not assessed - the client described no documents'];
  out.ai_notes = 'The submission arrived with an empty document description. No model call was made.';
} else if (unparseable) {
  // The two remaining cases read identically to the code and completely differently to a
  // paralegal: one means the model answered badly, the other that it never answered.
  out.ai_summary = callFailed
    ? 'The document review did not run - the model did not respond in time. The intake itself is safe and is recorded; a paralegal must review the documents by hand.'
    : 'AI parsing error - manual review required.';
  out.documents_missing = [callFailed ? 'Not assessed - review did not run' : 'Unable to parse AI response'];
  out.ai_notes = callFailed
    ? 'Model call failed: ' + callError
    : 'Raw response: ' + String(text).substring(0, 500);
}

// A score we could not read must not sit next to a label that asserts completeness.
// 0 out of 100 beside the word "Complete" is worse than admitting we do not know.
if (scoreUnreadable) {
  if (out.completeness_label !== 'Unknown') {
    violations.push('completeness_label ' + JSON.stringify(out.completeness_label) +
                    ' was dropped to Unknown because the score could not be read');
  }
  out.completeness_label = 'Unknown';
}

// Nothing the model returned can breach a contract it was never given. With no description
// there was no call, so the field-level complaints collected above - score not a number,
// label not in the domain - are about an answer that was never requested. Clearing them
// keeps `contract_violated` meaning what the firm reads it as: the classifier misbehaved.
if (nothingToReview) violations.length = 0;

// A contract violation is not the model's to report. It is ours to surface to a human.
out.contract_violated = violations.length > 0;
if (out.contract_violated) {
  out.priority_flag = true;
  out.ai_notes = (out.ai_notes ? out.ai_notes + ' | ' : '') + 'Output contract: ' + violations.join('; ');
}

// --- Checklist grounding, enforced rather than instructed -------------------
// The client is only ever shown documents the firm itself lists for this case type.
// Whatever the model invents or paraphrases is kept for the paralegal and never sent out.
const catalogue = $('Build DeepSeek Body').first().json.checklist || [];
// Diacritics are stripped rather than dropped. Half the firm's intake is written in
// Spanish, and [^a-z0-9] turned 'declaración' into 'declaraci n' — every accented word
// was silently cut in two, so no Spanish term could ever match anything.
const norm = (s) => String(s == null ? '' : s)
  .toLowerCase()
  .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
  .replace(/\([^)]*\)/g, ' ')
  .replace(/[^a-z0-9]+/g, ' ')
  .trim();
// A form number identifies one document unambiguously inside a single case type's
// catalogue, so 'I-130 receipt notice' is the I-130 petition. Matching on it here is
// deterministic, costs no tokens, and does not depend on the model agreeing.
const formCodes = (s) => {
  const out = [];
  const re = /(^| )([a-z]{1,2}) (\d{2,4})( |$)/g;
  let m;
  while ((m = re.exec(s)) !== null) { out.push(m[2] + m[3]); re.lastIndex -= 1; }
  return out;
};
const same = (a, b) => {
  const x = norm(a), y = norm(b);
  if (!x || !y) return false;
  if (x === y || x.indexOf(y) !== -1 || y.indexOf(x) !== -1) return true;
  const fx = formCodes(x), fy = formCodes(y);
  return fx.length === 1 && fy.length === 1 && fx[0] === fy[0];
};

// --- Evidence anchoring ----------------------------------------------------
// The model claiming a document is present is not the same as the client saying they
// have it. Asked about a case where the client wrote "my previous lawyer handled
// everything, I assume the whole package was submitted", the model returned all nine
// Family Visa documents as present: an inference about a third party, rendered as
// possession. Nothing above could tell the difference, because nothing above ever read
// what the client wrote.
//
// So a document is only shown as present if the client's own words carry an anchor for
// it — its form number, or a term for it in either language the firm accepts. Anchors
// only ever *remove* a claim; they never add one. The failure therefore moves to the
// recoverable side: at worst a client is asked to confirm something they already sent.
const ANCH = {
  'Petition I-130': ['peticion familiar', 'family petition'],
  'Proof of US Petitioner Status': ['citizenship', 'ciudadania', 'naturalization', 'naturalizacion',
    'green card', 'residencia', 'us citizen', 'ciudadano', 'us passport', 'pasaporte americano',
    'pasaporte estadounidense', 'estadounidense'],
  'Proof of Relationship': ['marriage certificate', 'acta de matrimonio', 'certificado de matrimonio',
    'marriage license', 'relationship', 'relacion', 'parentesco', 'photos together', 'fotos juntos',
    'joint account', 'cuenta conjunta', 'wedding', 'boda', 'casados', 'married'],
  'Beneficiary passport': ['passport', 'pasaporte'],
  'I-864 Affidavit of Support': ['affidavit of support', 'affidavit', 'declaracion jurada',
    'sustento economico', 'sponsor'],
  'DS-260 form': ['ds 260'],
  'Medical exam I-693': ['medical exam', 'examen medico', 'medical examination', 'examen de salud',
    'vaccination', 'vacunas'],
  'Police clearance certificates': ['police clearance', 'police certificate', 'antecedentes penales',
    'antecedentes policiales', 'certificado de policia', 'record policial', 'criminal record'],
  'Birth certificates for dependents': ['birth certificate', 'acta de nacimiento',
    'partida de nacimiento', 'certificado de nacimiento'],

  'I-140 approval notice': ['approval notice', 'aviso de aprobacion'],
  'Priority Date confirmation': ['priority date', 'fecha de prioridad'],
  'I-485 application': ['adjustment of status', 'ajuste de estatus'],
  'Employment verification letter': ['employment verification', 'verification letter',
    'carta de trabajo', 'carta laboral', 'carta de empleo', 'employment letter'],
  'Educational credentials': ['diploma', 'degree', 'transcript', 'titulo', 'universidad',
    'university', 'educational', 'estudios', 'bachelor', 'master'],
  'Professional certifications': ['certification', 'certificacion', 'license', 'licencia',
    'credential', 'colegiatura'],
  'Tax returns last 3 years': ['tax return', 'taxes', 'impuestos', 'declaracion de impuestos',
    'w 2', '1040', 'irs'],
  'Passport copies all pages': ['passport', 'pasaporte'],

  'I-589 application': ['asylum application', 'solicitud de asilo'],
  'Personal statement detailed': ['personal statement', 'declaracion personal', 'my story',
    'mi historia', 'declaracion escrita', 'written statement', 'affidavit', 'testimonio propio'],
  'Country condition evidence': ['country condition', 'condiciones del pais', 'informes del pais',
    'news article', 'articulos de prensa', 'human rights', 'derechos humanos', 'noticias',
    'situation in my country', 'situacion en mi pais', 'situation in my region'],
  'Police/medical/human rights reports': ['police report', 'denuncia', 'medical report',
    'informe medico', 'reporte medico', 'human rights', 'derechos humanos', 'hospital'],
  'Identity documents': ['identity', 'identidad', 'cedula', 'id card', 'birth certificate',
    'acta de nacimiento', 'passport', 'pasaporte', 'documento de identidad'],
  'Evidence of persecution': ['persecution', 'persecucion', 'threat', 'amenaza', 'tortura',
    'torture', 'evidence of', 'pruebas', 'attacked', 'atacaron'],
  'Witness statements if available': ['witness', 'testigo', 'testimonio'],
  'Photos of harm': ['photo', 'foto', 'picture', 'imagen', 'fotografia'],
  'Attorney G-28 form': ['notice of appearance'],
  'Translations of non-English documents': ['translation', 'traduccion', 'traducido', 'translated',
    'certified translation'],

  'I-751 if applicable': ['removal of conditions', 'remover condiciones', 'conditional resident'],
  'Green card copy both sides': ['green card', 'tarjeta verde', 'tarjeta de residencia',
    'permanent resident card', 'residencia permanente', 'i 551', 'mica'],
  'Passport photos x2': ['passport photo', 'foto tipo pasaporte', 'photographs', 'fotos',
    'photos', 'foto carnet'],
  'Tax returns last 5 years': ['tax return', 'taxes', 'impuestos', 'declaracion de impuestos',
    'w 2', '1040', 'irs'],
  'N-400 application': ['naturalization application', 'solicitud de ciudadania'],
  'Travel history documentation': ['travel', 'historial de viajes', 'trip', 'viaje',
    'salidas del pais', 'abroad', 'en el extranjero', 'entradas y salidas'],
  'Selective Service proof': ['selective service', 'servicio selectivo'],
  'Criminal history disclosure': ['criminal', 'arrest', 'arresto', 'antecedentes', 'dui',
    'citation', 'multa', 'corte', 'court', 'convicted', 'condena'],
  'Employment history last 5 years': ['employment history', 'historial laboral', 'work history',
    'trabajos', 'empleos', 'jobs', 'employers', 'empleadores']
};

const descNorm  = norm(fields.document_description);
const descCodes = formCodes(descNorm);
const grounded = (c) => {
  // The form number is the strongest anchor there is: it names one document and cannot
  // be a paraphrase of another.
  if (formCodes(norm(c)).some(k => descCodes.indexOf(k) !== -1)) return true;
  return (ANCH[c] || []).some(a => descNorm.indexOf(a) !== -1);
};

const claimedPresent = out.documents_present;
const claimed = catalogue.filter(c => claimedPresent.some(m => same(c, m)));
const catalogue_present  = claimed.filter(grounded);
const catalogue_inferred = claimed.filter(c => catalogue_present.indexOf(c) === -1);
const catalogue_missing = catalogue.filter(c => catalogue_present.indexOf(c) === -1);

out.catalogue_claimed  = claimed;
out.catalogue_inferred = catalogue_inferred;
if (catalogue_inferred.length) {
  out.priority_flag = true;
  out.ai_notes = (out.ai_notes ? out.ai_notes + ' | ' : '') +
    'Claimed present but not stated by the client, so still requested: ' +
    catalogue_inferred.join('; ');
}

const everythingClaimed = claimedPresent
  .concat(out.documents_missing)
  .concat(out.documents_optional);
const offcatalogue = everythingClaimed.filter(
  m => !catalogue.some(c => same(c, m))
);

// A review that never ran has nothing missing and nothing present. Letting the filter run
// would mark the whole catalogue missing, and telling a client they are short of all ten
// documents when in fact nobody looked is worse than telling them nothing.
out.catalogue_present = out.review_ran ? catalogue_present : [];
out.catalogue_missing = out.review_ran ? catalogue_missing : [];
out.offcatalogue = out.review_ran ? offcatalogue : [];
out.catalogue_size = catalogue.length;

if (offcatalogue.length) {
  out.ai_notes = (out.ai_notes ? out.ai_notes + ' | ' : '') +
    'Outside the firm checklist, not shown to the client: ' + offcatalogue.join('; ');
}

// --- The score is arithmetic, not an opinion ---------------------------------
// Completeness against a checklist is a count over a total, and the system already holds
// both numbers. Taking the percentage from the model instead meant nothing ever compared
// it to the list beside it: a run could report 100 out of 100 with one document ticked,
// and did — under a prompt injection, and unremarked. Measured over 93 captured responses,
// the model's own label and its own score also disagree with each other constantly:
// 'Incomplete' spanned 30-67 while 'Critically Incomplete' spanned 0-44.
//
// So both are derived here, from one number, and can no longer contradict each other or
// the list. The model's figures are kept beside them for the paralegal, never shown to
// the client, and their divergence is recorded rather than assumed small.
const LABEL_BANDS = [[100, 'Complete'], [70, 'Mostly Complete'], [30, 'Incomplete'], [0, 'Critically Incomplete']];

out.model_completeness_score = out.completeness_score;
out.model_completeness_label = out.completeness_label;

if (!out.review_ran) {
  // Nothing was assessed, by either route. An empty present list is absence of an answer
  // and not an answer of zero; deriving 0 would turn a non-event into a confident verdict.
  out.completeness_score = null;
  out.completeness_label = 'Unknown';
} else if (catalogue.length === 0) {
  // 'Other' has no checklist. A percentage of an empty catalogue is not a low score, it
  // is an undefined one, and 0 and 100 are both lies about it.
  out.completeness_score = null;
  out.completeness_label = 'Unknown';
  out.ai_notes = (out.ai_notes ? out.ai_notes + ' | ' : '') +
    'No checklist exists for this case type, so completeness is undefined rather than low. ' +
    'The model offered ' + out.model_completeness_score + '.';
  out.priority_flag = true;
} else {
  const derived = Math.round(100 * catalogue_present.length / catalogue.length);
  out.completeness_score = derived;
  for (let i = 0; i < LABEL_BANDS.length; i++) {
    if (derived >= LABEL_BANDS[i][0]) { out.completeness_label = LABEL_BANDS[i][1]; break; }
  }
  out.score_basis = catalogue_present.length + ' of ' + catalogue.length;
  // A wide gap is not an error to correct — the derived figure already stands — but it is
  // the clearest signal available that the model and the checklist read the same intake
  // differently, and a paralegal should see which.
  const gap = scoreUnreadable ? null : Math.abs(derived - out.model_completeness_score);
  if (gap !== null && gap >= 25) {
    out.priority_flag = true;
    out.ai_notes = (out.ai_notes ? out.ai_notes + ' | ' : '') +
      'The model scored this ' + out.model_completeness_score + ' where the checklist gives ' +
      derived + ' (' + out.score_basis + ').';
  }
}

return [{ json: { ...fields, ...out, deepseek_raw_response: raw } }];
