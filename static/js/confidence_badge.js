// Milestone 25 Workstream 2: the one shared "how sure is the AI, really"
// component for JS-rendered pages. Every confidence/probability/signal-
// strength number on a page should be rendered through
// renderConfidenceBadge() instead of interpolated as a bare "${value}%"
// -- see templates/_confidence_badge.html for the server-rendered
// (Jinja) equivalent used by pure-page-reload templates, and
// agents/trading_intelligence/ai_trading_engine.py's own Recommendation
// dataclass docstring for where the underlying `confidence` vs
// `probability` distinction this encodes comes from.
//
// `kind` must be one of:
//   'raw_score'              -- an uncalibrated 0-100 heuristic
//                                signal-strength score (oi_engine / V3 /
//                                S-R / NTM / institutional scoring).
//                                This is NEVER a probability of
//                                anything -- always rendered with a
//                                fixed caveat, never as a bare
//                                percentage, so it can never be misread
//                                as "80% confidence = 80% probability
//                                of profit".
//   'calibrated_probability' -- a real historical-win-rate-backed
//                                probability (ai_trading_engine.
//                                Recommendation.probability, sourced
//                                from calibration_report(), which only
//                                ever returns a number once the sample
//                                size is >= CALIBRATION_MIN_SAMPLE).
//                                Renders the percentage (plus its
//                                sample size) only when present; falls
//                                back to an honest "not yet calibrated"
//                                explanation built from `note`
//                                otherwise -- never fabricates a number
//                                to fill the gap.
function renderConfidenceBadge({ value, kind, note, sampleSize, label } = {}) {
  // `label` distinguishes "not passed" (use the kind's default title) from
  // an explicit '' (the caller's own markup -- e.g. a table's own <td>
  // header -- already supplies the label, so render just the value/caveat).
  const defaultTitle = kind === 'calibrated_probability' ? 'Probability' : 'Confidence';
  const title = (label === undefined || label === null) ? defaultTitle : label;
  const prefix = title ? (_confBadgeEsc(title) + ': ') : '';
  if (kind === 'calibrated_probability') {
    if (value === null || value === undefined) {
      return '<span class="mythos-conf-badge mythos-conf-badge-uncalibrated" title="'
        + _confBadgeEscAttr(note || 'not enough historical trades yet to calibrate')
        + '">' + prefix + 'not yet calibrated</span>';
    }
    const sampleText = (sampleSize !== null && sampleSize !== undefined)
      ? ' (' + sampleSize + ' historical trade' + (sampleSize === 1 ? '' : 's') + ')' : '';
    return '<span class="mythos-conf-badge mythos-conf-badge-calibrated" title="Historical win rate' + _confBadgeEscAttr(sampleText) + '">'
      + prefix + value + '%' + _confBadgeEsc(sampleText) + '</span>';
  }
  // raw_score (default) -- always caveated, never presented as a probability.
  const shown = (value === null || value === undefined) ? '—' : (value + '%');
  return '<span class="mythos-conf-badge mythos-conf-badge-raw" title="Rule-based signal-strength score, not a calibrated win probability">'
    + prefix + _confBadgeEsc(shown)
    + ' <span class="mythos-conf-badge-caveat">(signal strength, not a win probability)</span></span>';
}

function _confBadgeEsc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function _confBadgeEscAttr(s) {
  return _confBadgeEsc(s).replace(/"/g, '&quot;');
}
