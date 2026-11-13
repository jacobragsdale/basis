/* basis's script for every Dash app, served from the app's origin (basis.dash.assets), after the app's own assets/.
   It adds to the objects Dash and dash-ag-grid read, and never replaces one, since an app's own script may have made
   it first, and every name it adds is basis's own. */
(function () {
  "use strict";

  var KEY = "mantine-color-scheme-value"; /* where Mantine's provider reads the scheme when a page loads */
  var clientside = (window.dash_clientside = window.dash_clientside || {});
  var basis = (clientside.basis = clientside.basis || {});

  /* The light and dark switch: flips the scheme on <html>, keeps the choice, and answers the switch's own label. */
  basis.scheme = function (clicks) {
    var root = document.documentElement;
    var shown = root.getAttribute("data-mantine-color-scheme") === "dark" ? "dark" : "light";
    if (clicks) {
      shown = shown === "dark" ? "light" : "dark";
      root.setAttribute("data-mantine-color-scheme", shown);
      try {
        window.localStorage.setItem(KEY, shown);
      } catch (refused) {
        /* a browser that keeps nothing still switches */
      }
    }
    return shown === "dark" ? "Switch to the light scheme" : "Switch to the dark scheme";
  };

  /* One number, one text: basis.core.display's formatter, ported, so a cell rounds as the server, a message and Excel
     do. A float is first rounded to 15 significant digits, half away from zero, from its exact decimal value, then
     scaled exactly (percent, basis points, K/M/B), then rounded once to the decimals shown. A price in 32nds is read
     at a float's shortest digits, rounded to 8 decimals, then to its step. The spec is what basis.dash.text.spec writes
     for a column's display format, every default already resolved. */
  var SIGNIFICANT = 15;
  var PRICE_DECIMALS = 8;
  var EIGHTHS_PER_POINT = 256n;
  var SHIFTS = { K: 3, M: 6, B: 9 };
  var SUFFIXES = { amount: { K: "K", M: "M", B: "B" }, par: { K: "M", M: "MM", B: "BN" } };
  var STEPS = { "thirty-seconds": 1, halves: 2, quarters: 4, eighths: 8 };
  var SIGNS = { minus: ["-", ""], parentheses: ["(", ")"] };
  var BP = "\u00a0bp";
  var UNDEFINED = "\u2014";
  var NULL = "";

  function power(exponent) {
    return 10n ** BigInt(exponent);
  }

  /* digits * 10**exponent, not negative, rounded to `decimals` places half away from zero, scaled by 10**decimals. */
  function roundHalfAway(digits, exponent, decimals) {
    var shift = exponent + decimals;
    if (shift >= 0) return digits * power(shift);
    var divisor = power(-shift);
    var quotient = digits / divisor;
    return 2n * (digits % divisor) >= divisor ? quotient + 1n : quotient;
  }

  /* A positive finite double rounded to 15 significant digits, from its exact value: [digits, exponent]. */
  function fifteenDigits(magnitude) {
    var view = new DataView(new ArrayBuffer(8));
    view.setFloat64(0, magnitude);
    var high = view.getUint32(0);
    var biased = (high >>> 20) & 0x7ff;
    var mantissa = (BigInt(high & 0xfffff) << 32n) | BigInt(view.getUint32(4));
    var binary = -1074;
    if (biased !== 0) {
      mantissa |= 1n << 52n;
      binary = biased - 1075;
    }
    var exact = binary >= 0 ? mantissa << BigInt(binary) : mantissa * 5n ** BigInt(-binary);
    var scale = binary >= 0 ? 0 : -binary; /* the value is exact * 10**-scale */
    var dropped = exact.toString().length - SIGNIFICANT;
    if (dropped <= 0) return [exact, -scale];
    return [roundHalfAway(exact, -dropped, 0), dropped - scale];
  }

  /* A positive finite double's shortest text that reads back as itself, as JavaScript's String writes it. */
  function shortest(magnitude) {
    var parts = magnitude.toExponential().split("e");
    var mantissa = parts[0].split(".");
    var fraction = mantissa[1] || "";
    return [BigInt(mantissa[0] + fraction), Number(parts[1]) - fraction.length];
  }

  function signed(body, negative, spec) {
    if (!negative) return body;
    var sign = SIGNS[spec.negative || "minus"];
    return sign[0] + body + sign[1];
  }

  function grouped(whole) {
    return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function decimalsOf(spec, natural) {
    if (spec.decimals !== undefined && spec.decimals !== null) return spec.decimals;
    if (spec.percent) return 2;
    return spec.abbreviate ? 1 : natural;
  }

  function number(digits, exponent, decimals, negative, spec) {
    exponent += (spec.percent ? 2 : 0) + (spec.bps ? 4 : 0) - (spec.abbreviate ? SHIFTS[spec.abbreviate] : 0);
    var rounded = roundHalfAway(digits, exponent, decimals);
    var scale = power(decimals);
    var whole = (rounded / scale).toString();
    var body = spec.thousands === false ? whole : grouped(whole);
    if (decimals > 0) body += "." + (rounded % scale).toString().padStart(decimals, "0");
    if (spec.abbreviate) body += SUFFIXES[spec.suffixes || "amount"][spec.abbreviate];
    body += (spec.percent ? "%" : "") + (spec.bps ? BP : "");
    return signed(body, negative && rounded !== 0n, spec);
  }

  function thirtySeconds(digits, exponent, step, negative, spec) {
    var perPoint = BigInt(32 * STEPS[step]);
    var eighths = roundHalfAway(digits * perPoint, exponent, 0) * (EIGHTHS_PER_POINT / perPoint);
    var rest = eighths % EIGHTHS_PER_POINT;
    var eighth = rest % 8n;
    var mark = eighth === 0n ? "" : eighth === 4n ? "+" : eighth.toString();
    var body = (eighths / EIGHTHS_PER_POINT).toString() + "-" + (rest / 8n).toString().padStart(2, "0") + mark;
    return signed(body, negative && eighths !== 0n, spec);
  }

  /* A float's text in `spec`: `basisFormat(params.value, {...})` in a column's valueFormatter. A missing value, null
     as JSON carries NaN, is the empty text, as the server shows it. */
  function format(value, spec) {
    spec = spec || {};
    if (value === null || value === undefined || value === "") return NULL;
    if (typeof value !== "number") return String(value);
    var shown;
    if (Number.isNaN(value)) {
      shown = UNDEFINED;
    } else {
      var negative = value < 0 || Object.is(value, -0);
      var magnitude = Math.abs(value);
      if (spec.thirtySeconds) {
        if (!Number.isFinite(value)) return UNDEFINED;
        var short = shortest(magnitude);
        shown = thirtySeconds(roundHalfAway(short[0], short[1], PRICE_DECIMALS), -PRICE_DECIMALS, spec.thirtySeconds, negative, spec);
      } else if (!Number.isFinite(value)) {
        shown = signed("\u221e" + (spec.bps ? BP : ""), negative, spec);
      } else {
        var found = value === 0 ? [0n, 0] : fifteenDigits(magnitude);
        shown = number(found[0], found[1], decimalsOf(spec, 2), negative, spec);
      }
    }
    if (!spec.symbol || shown === UNDEFINED) return shown;
    var sign = shown[0] === "-" || shown[0] === "(" ? shown[0] : "";
    return sign + spec.symbol + shown.slice(sign.length);
  }

  basis.format = format;
  var grid = (window.dashAgGridFunctions = window.dashAgGridFunctions || {});
  grid.basisFormat = format;
})();
