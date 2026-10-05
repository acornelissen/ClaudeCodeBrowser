// Credential detection: the one definition of "this field, or this name,
// holds a credential", shared by every part of ClaudeCodeBrowser that decides
// it.
//
//  - content.js, the field guard: refuse to type into a credential field and
//    mask one on read.
//  - background.js, the scrubber: credential-shaped names in captured bodies,
//    URLs, headers and HTML.
//  - mcp-server/headless_backend.py, which reads this file and runs it in
//    the page, so headless and Firefox answer from the same code.
//
// These used to be three hand-kept copies held together by tests, and most
// of the leaks found in October 2026 were one copy behind another.
//
// A plain script, no module syntax: the manifest loads it ahead of content.js
// and background.js, which share its scope, and headless interpolates it
// into a function body. var and function declarations rather than const, so
// a second load into the same scope cannot throw.

// autocomplete is a space-separated, case-insensitive token list, so
// "Current-Password", "current-password " and the spec-legal
// "section-login current-password" all have to match. Beyond passwords,
// one-time codes and card fields are credentials too: they are not
// type=password, so nothing else would protect them.
var CREDENTIAL_AUTOCOMPLETE_TOKENS = new Set([
  'current-password', 'new-password', 'one-time-code',
  'cc-number', 'cc-csc', 'cc-exp', 'cc-exp-month', 'cc-exp-year'
]);

// A field's name or id is the third signal, and on real pages often the only
// one: <input type="text" name="passwd"> is a password field that says
// type="text", and a contenteditable <div id="otp-code"> is a credential with
// no type at all. The same names are scrubbed out of captured traffic.
//
// Deliberately loose, because masking a field called "author" costs less
// than handing a credential to the agent in clear. Match through
// looksLikeCredentialName(), never directly: it handles camelCase
// boundaries, which this pattern cannot. The boundaries are the fiddly part
// and have been got wrong in both directions, so they are spelt out:
//
//  - `otp`, `auth` and `session` need only a TRAILING boundary. That excludes
//    `author`, `authority` and `notPublished`, where a letter follows, while
//    still allowing `userauth` and `userotp`, where the name ends there.
//  - `pass` and `pin` need BOTH, because `bypass`, `compass` and `spin` end
//    in them. The credential compounds are listed by hand: `passkey`,
//    `userpass`, `pincode`.
//  - `ssn` needs NEITHER: the camelCase step already breaks the words that
//    made it look dangerous (`className` becomes `class_Name`), while
//    anchoring it lost `SSNNumber` and `userssn`.
//  - `cc` and `card` codes need a LEADING boundary, so `accNumber` and
//    `discardCode` stay ordinary.
//  - `totp`, `hotp` and `sessid` have no boundary at all and are listed.
var CREDENTIAL_NAME_RE =
  /(pass(?:word|wd|phrase|code|key)|userpass|(?:^|[^a-z])pass(?:[^a-z]|$)|pwd|secret|token|credential|one[-_]?time[-_]?code|[th]?otp(?:[^a-z]|$)|oauth|authorization|authenticat|auth(?:z|n)(?:[^a-z]|$)|auth[-_]?(?:token|key|code|header|secret|data)|auth(?:[^a-z]|$)|api[-_]?key|private[-_]?key|session[-_]?(?:id|token|key|secret|value)|sess[-_]?id|session(?:[^a-z]|$)|sessid|cvv|cvc|card[-_]?number|jwt|bearer|signature|ssn|(?:^|[^a-z])pin(?:[^a-z]|$)|mfa[-_]?code|verification[-_]?code|security[-_]?code|(?:^|[^a-z])cc[-_]?number|credit[-_]?card|(?:^|[^a-z])pin[-_]?code|cookie|recovery[-_]?codes?|backup[-_]?codes?|(?:^|[^a-z])card[-_]?code)/i;

// How many nested credential fields one whole-page text read will mask. Only
// there to bound the work; 50 was low enough that a busy page leaked the rest
// silently, so it is higher and the read reports when it bites.
var MAX_SCRUBBED_FIELDS = 500;

// The anchors in CREDENTIAL_NAME_RE only see a non-letter as a boundary, so
// camelCase is turned into separators first: without it otpCode,
// sessionValue, authData and pinCode do not match at all.
function looksLikeCredentialName(name) {
  return CREDENTIAL_NAME_RE.test(
    String(name == null ? '' : name).replace(/([a-z0-9])([A-Z])/g, '$1_$2'));
}

function attributeOf(element, name) {
  if (!element || typeof element.getAttribute !== 'function') return null;
  return element.getAttribute(name);
}

// The name/id rule is only for elements that hold a value somebody entered.
// Applied to everything, it would mask the text of any
// <div id="user-session-banner"> on the page.
function holdsEnteredValue(element) {
  const tag = element.tagName || '';
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (element.isContentEditable === true) return true;
  // A custom element - its tag name must contain a hyphen - is how a
  // component library ships a field: <sl-input>, <ion-input>,
  // <vaadin-password-field>.
  return tag.includes('-');
}

function isPasswordField(element) {
  if (!element) return false;
  // Not restricted to <input>: Shoelace, Ionic and Vaadin wrap a real input
  // in a shadow root, so <sl-input type="password"> is the only element an
  // agent can target.
  if (element.type === 'password') return true;
  if ((attributeOf(element, 'type') || '').toLowerCase() === 'password') return true;

  const autocomplete = attributeOf(element, 'autocomplete');
  if (autocomplete && autocomplete
        .toLowerCase()
        .split(/\s+/)
        .some(token => CREDENTIAL_AUTOCOMPLETE_TOKENS.has(token))) {
    return true;
  }

  if (!holdsEnteredValue(element)) return false;

  // name can be a form path like user[password], which still names a
  // credential, so this is a substring match rather than an equality test.
  const name = element.name || attributeOf(element, 'name') || '';
  const id = element.id || attributeOf(element, 'id') || '';
  return looksLikeCredentialName(name) || looksLikeCredentialName(id);
}

// Hidden inputs routinely carry CSRF tokens, session ids and order ids, and
// are never something the agent needs the value of - but writing one is how a
// form carries state, so only the read mask includes them.
function isConcealedValueField(element) {
  return isPasswordField(element) ||
    (!!element && element.tagName === 'INPUT' && element.type === 'hidden');
}

// Whether a credential field holds anything, without reading it out. A
// contenteditable has no .value, so judging by .value alone called a filled
// one empty.
function holdsEnteredText(element) {
  const raw = 'value' in element ? element.value : element.textContent;
  return raw !== undefined && raw !== null && String(raw).length > 0;
}

// Mask the text of credential fields nested inside a block of text read from
// root - a whole-page read defaults to <body>, which would otherwise return a
// <div contenteditable> PIN in the middle of the page dump. Returns
// {text, masked, fields, capped}: masked counts distinct secrets replaced
// (two fields holding one value are one secret), fields counts the fields.
//
// An <input> contributes nothing to innerText whatever its value, so inputs
// cannot leak this way; and a credential that reached the page as ordinary
// prose is not something this can find. Whether to run at all - the
// allow_password_typing override - is the caller's decision.
function scrubNestedCredentials(text, root) {
  if (!text) return { text, masked: 0, fields: 0, capped: false };
  let capped = false;
  let candidates;
  try {
    // Filter FIRST, then cap. Capping the raw [contenteditable] list meant a
    // Notion- or CMS-style page with 50 ordinary editable cells followed by
    // one credential field never reached the credential field at all. The
    // cap is on how many credentials are masked, not on how many elements
    // are looked at.
    const all = root.querySelectorAll
      ? Array.from(root.querySelectorAll('[contenteditable], textarea'))
          .filter(el => el !== root && isPasswordField(el))
      : [];
    capped = all.length > MAX_SCRUBBED_FIELDS;
    candidates = all.slice(0, MAX_SCRUBBED_FIELDS);
  } catch (e) {
    candidates = [];
  }

  const secrets = [];
  for (const field of candidates) {
    const own = field.tagName === 'TEXTAREA'
      ? (field.value || '')
      : (typeof field.innerText === 'string'
          ? field.innerText
          : (field.textContent || ''));
    const secret = own.trim();
    // A one- or two-character "secret" is not worth masking every
    // occurrence of across a whole page.
    if (secret.length >= 3) secrets.push(secret);
  }

  // Longest first. Masking a shorter secret that is a PREFIX of a longer one
  // destroys the longer one's text and leaves its tail behind: "SSS1" and
  // "SSS10" came out as "*** ***0".
  secrets.sort((a, b) => b.length - a.length);

  let out = text;
  let masked = 0;
  for (const secret of secrets) {
    if (!out.includes(secret)) continue;
    out = out.split(secret).join('***');
    masked++;
  }
  return { text: out, masked, fields: secrets.length, capped };
}
