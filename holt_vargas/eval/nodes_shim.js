// Executes the two n8n Code nodes that make up the classifier, verbatim.
//
// The files under deployed/ are copied from the live workflow and hashed in SHA256SUMS.
// Nothing here reimplements their logic: the eval has to fail when the deployed node
// changes, and a reimplementation would quietly keep passing.
//
//   node nodes_shim.js prompt  < [{case_type, document_description}, ...]
//   node nodes_shim.js parse   < [{content, fields, checklist}, ...]

const fs = require('fs');
const path = require('path');

const BUILD = fs.readFileSync(path.join(__dirname, 'deployed/build_deepseek_body.js'), 'utf8');
const PARSE = fs.readFileSync(path.join(__dirname, 'deployed/parse_deepseek_response.js'), 'utf8');

function readStdin() {
  return JSON.parse(fs.readFileSync(0, 'utf8'));
}

// Both nodes reach for sibling nodes through $('Name'). The shim supplies exactly the
// fields those calls read, and throws on any node name the code did not use before, so a
// new dependency in the deployed code surfaces here instead of being silently defaulted.
function makeCtx(map) {
  return (name) => {
    if (!(name in map)) throw new Error('shim: unexpected node reference ' + JSON.stringify(name));
    return { first: () => ({ json: map[name] }) };
  };
}

const mode = process.argv[2];
const input = readStdin();

if (mode === 'prompt') {
  const out = input.map((c) => {
    const fields = {
      full_name: c.full_name || 'Eval Case',
      email: 'eval@example.invalid',
      phone: '',
      case_type: c.case_type,
      document_description: c.document_description,
      country_of_origin: c.country_of_origin || '',
      source_event_id: c.id || 'eval'
    };
    const fn = new Function('$input', '$', BUILD);
    const r = fn({ first: () => ({ json: fields }) }, makeCtx({ 'Set Fields': fields }))[0].json;
    return { id: c.id, deepseek_body: r.deepseek_body, checklist: r.checklist, fields };
  });
  process.stdout.write(JSON.stringify(out));

} else if (mode === 'parse') {
  const out = input.map((c) => {
    const modelResponse = {
      choices: [{ message: { content: c.content } }],
      usage: c.usage || {}
    };
    const fn = new Function('$input', '$', PARSE);
    const r = fn(
      { first: () => ({ json: modelResponse }) },
      makeCtx({ 'Set Fields': c.fields, 'Build DeepSeek Body': { checklist: c.checklist } })
    )[0].json;
    delete r.deepseek_raw_response;
    return { id: c.id, parsed: r };
  });
  process.stdout.write(JSON.stringify(out));

} else {
  console.error('usage: node nodes_shim.js prompt|parse  < cases.json');
  process.exit(2);
}
