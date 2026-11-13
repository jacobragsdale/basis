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

  /* The largest magnitude of a column, over the rows the grid was given: cached by the rows' array, which a new answer
     replaces. */
  var largest = new WeakMap();
  function magnitude(params) {
    var rows = params.api.getGridOption("rowData") || [];
    var field = params.colDef.field;
    var known = largest.get(rows);
    if (!known) largest.set(rows, (known = {}));
    if (!(field in known)) {
      var most = 0;
      for (var i = 0; i < rows.length; i++) {
        var value = rows[i][field];
        if (typeof value === "number" && Number.isFinite(value) && Math.abs(value) > most) most = Math.abs(value);
      }
      known[field] = most;
    }
    return known[field];
  }

  /* A cell's bar or heat colour behind its number, against its column's largest magnitude; none in the totals row. */
  var HEAT_MOST = 24; /* percent of the colour at the largest value, so the text stays legible over it */
  function scale(params, kind) {
    var value = params.value;
    if (params.node.rowPinned || typeof value !== "number" || !Number.isFinite(value) || value === 0) return null;
    var most = magnitude(params);
    if (!most) return null;
    var share = Math.min(Math.abs(value) / most, 1);
    if (kind === "bar") {
      var colour = value < 0 ? "var(--basis-loss)" : "var(--basis-primary)";
      var length = (share * 100).toFixed(2) + "%";
      return {
        backgroundImage: "linear-gradient(90deg, color-mix(in oklch, " + colour + " 20%, transparent) " + length + ", transparent " + length + ")",
        backgroundSize: "100% 60%",
        backgroundPosition: "0 50%",
        backgroundRepeat: "no-repeat",
      };
    }
    var pole = value < 0 ? "var(--basis-div-10)" : "var(--basis-div-2)";
    return { backgroundColor: "color-mix(in oklch, " + pole + " " + (share * HEAT_MOST).toFixed(1) + "%, transparent)" };
  }

  /* A grid's quick filter: what the box holds, given to the grid in the browser alone. */
  basis.search = function (text, id) {
    var grids = window.dash_ag_grid;
    if (grids && grids.getApiAsync && id) {
      grids.getApiAsync(id.grid).then(
        function (api) {
          api.setGridOption("quickFilterText", text || "");
        },
        function () {
          /* the grid is gone: nothing to filter */
        },
      );
    }
    return window.dash_clientside.no_update;
  };

  /* A grid's totals are of the rows shown. The server's stand while nothing is filtered; while a quick filter or a
     column's filter is on, the declared totals are worked out here over the rows the filter keeps, and the server's
     come back when it is cleared. The grid's context holds what was declared (basis.dash.grids). */
  function sum(values) {
    var total = 0;
    var lost = 0; /* Neumaier's compensation, so a long column sums as the server's fsum does to 15 digits */
    for (var i = 0; i < values.length; i++) {
      var next = total + values[i];
      lost += Math.abs(total) >= Math.abs(values[i]) ? total - next + values[i] : values[i] - next + total;
      total = next;
    }
    return total + lost;
  }

  function given(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function totalOf(rows, field, kind) {
    var values = [];
    if (kind && typeof kind === "object") {
      var products = [];
      var weights = [];
      for (var w = 0; w < rows.length; w++) {
        var value = rows[w][field];
        var weight = rows[w][kind.weighted];
        if (given(value) && given(weight)) {
          products.push(value * weight);
          weights.push(weight);
        }
      }
      var weighed = sum(weights);
      return weighed !== 0 ? sum(products) / weighed : null;
    }
    for (var i = 0; i < rows.length; i++) if (given(rows[i][field])) values.push(rows[i][field]);
    if (kind === "count") return values.length;
    if (!values.length) return null;
    if (kind === "sum") return sum(values);
    if (kind === "mean") return sum(values) / values.length;
    return kind === "min" ? Math.min.apply(null, values) : Math.max.apply(null, values);
  }

  /* The totals row of `rows` by `declared`: {totals: {FIELD: KIND}, label, key}, as the server writes it. */
  function totalsOf(rows, declared) {
    var row = {};
    Object.keys(declared.totals).forEach(function (field) {
      row[field] = totalOf(rows, field, declared.totals[field]);
    });
    if (declared.label) row[declared.label] = "Total";
    if (declared.key && !(declared.key in row)) row[declared.key] = "";
    return row;
  }

  var grids = new WeakMap();
  function totals(event) {
    var api = event.api;
    var declared = (api.getGridOption("context") || {}).basis;
    if (!declared) return;
    var state = grids.get(api);
    if (!state) grids.set(api, (state = { mine: null, server: null, busy: false }));
    if (state.busy) return;

    var current = api.getGridOption("pinnedBottomRowData");
    if (current !== state.mine) state.server = current;
    var all = 0;
    var shown = [];
    api.forEachNode(function () {
      all += 1;
    });
    api.forEachNodeAfterFilter(function (node) {
      shown.push(node.data);
    });
    var filtered = api.isAnyFilterPresent();
    var next = null;
    if (filtered && Object.keys(declared.totals).length) {
      var row = totalsOf(shown, declared);
      if (current !== state.mine || JSON.stringify(state.mine[0]) !== JSON.stringify(row)) next = [row];
    } else if (state.mine && current === state.mine) {
      next = state.server || [];
    }
    if (next) {
      state.mine = filtered ? next : null;
      state.busy = true;
      try {
        api.setGridOption("pinnedBottomRowData", next);
      } finally {
        state.busy = false;
      }
    }
    if (declared.count && window.dash_clientside.set_props) {
      var whole = format(all, { decimals: 0 });
      var text = filtered ? format(shown.length, { decimals: 0 }) + " of " + whole : whole;
      window.dash_clientside.set_props(declared.count, { children: text + " " + declared.noun });
    }
  }

  /* A page's freshness line, from the status store: when its data was read, the oldest of its datasets, in the desk's
     zone, with the date where it is not today's; or which of them is stale, a last good value a failed reload left. Its
     title lists each dataset. An absolute time, so the line never ticks. */
  var ZONE = "America/New_York"; /* basis.core.display.DEFAULT_ZONE */
  var clock = new Intl.DateTimeFormat("en-US", { timeZone: ZONE, hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", timeZoneName: "short" });
  var calendarDay = new Intl.DateTimeFormat("en-CA", { timeZone: ZONE, year: "numeric", month: "2-digit", day: "2-digit" });
  function when(iso, now) {
    var moment = new Date(iso);
    if (Number.isNaN(moment.getTime())) return "";
    var day = calendarDay.format(moment);
    return (day === calendarDay.format(now) ? "" : day + " ") + clock.format(moment);
  }

  basis.freshness = function (data, now) {
    var context = window.dash_clientside.callback_context;
    var outputs = (context && context.outputs_list && context.outputs_list[0]) || [];
    var today = now ? new Date(now) : new Date();
    var known = data || {};
    var texts = [];
    var classes = [];
    var titles = [];
    outputs.forEach(function (output) {
      var names = output.id.datasets ? output.id.datasets.split(",") : Object.keys(known).sort();
      var found = names.filter(function (name) {
        return known[name] && known[name].loaded_at;
      });
      if (!found.length) {
        texts.push("");
        classes.push("basis-freshness");
        titles.push("");
        return;
      }
      var oldest = found.reduce(function (first, name) {
        return known[name].loaded_at < known[first].loaded_at ? name : first;
      }, found[0]);
      var stale = found.filter(function (name) {
        return known[name].stale;
      });
      var title = found.map(function (name) {
        var entry = known[name];
        return name + ": " + format(entry.rows, { decimals: 0 }) + " rows, read " + when(entry.loaded_at, today) + (entry.stale ? ", stale" : "");
      });
      if (stale.length) {
        texts.push("Stale: " + stale.join(", ") + ", as read " + when(known[stale[0]].loaded_at, today));
        classes.push("basis-freshness basis-stale");
      } else {
        texts.push("Data as of " + when(known[oldest].loaded_at, today));
        classes.push("basis-freshness");
      }
      titles.push(title.join("\n"));
    });
    return [texts, classes, titles];
  };

  /* A filter's value, written to the URL's query string as it changes, in place, so the page does not navigate, no
     request leaves it, and a link reproduces the view: a list joined by commas, a range's two days by "..", and an
     empty value taken out. Its key is the filter's id (basis.dash.filters). */
  function queryText(value) {
    if (value === null || value === undefined || value === "") return null;
    if (Array.isArray(value)) {
      var given = value.filter(function (part) {
        return part !== null && part !== undefined && part !== "";
      });
      if (!given.length) return null;
      return value.length === 2 && /^\d{4}-\d{2}-\d{2}/.test(String(given[0])) ? given.length === 2 ? given[0].slice(0, 10) + ".." + given[1].slice(0, 10) : undefined : given.join(",");
    }
    return String(value);
  }

  basis.remember = function () {
    var context = window.dash_clientside.callback_context;
    var url = new URL(window.location.href);
    (context.triggered || []).forEach(function (changed) {
      var at = changed.prop_id.lastIndexOf(".");
      var key = changed.prop_id.slice(0, at);
      var text = queryText(changed.value);
      if (text === undefined) return; /* a range half chosen: wait for its last day */
      if (text === null) url.searchParams.delete(key);
      else url.searchParams.set(key, text);
    });
    var href = url.href.replace(/%2C/g, ","); /* a list reads as one in a link: book=Rates,Credit */
    if (href !== window.location.href) window.history.replaceState(window.history.state, "", href);
    return window.dash_clientside.no_update;
  };

  /* A component's marks (basis.dash.states): stale while the status store marks its dataset so; not updated from a
     failed answer until its next one, which this clears. */
  var MARK_HIDDEN = "basis-mark basis-mark-hidden";
  basis.staleness = function (data) {
    var context = window.dash_clientside.callback_context;
    var outputs = (context && context.outputs_list) || [];
    var known = data || {};
    return outputs.map(function (output) {
      var entry = known[output.id.dataset];
      return entry && entry.stale ? "basis-mark basis-mark-stale" : MARK_HIDDEN;
    });
  };
  var answered = {}; /* each component that has shown an answer, by id */
  basis.failedBody = function (mark, body) {
    var context = window.dash_clientside.callback_context;
    var output = context && context.outputs_list;
    var shown = String(body || "").indexOf("basis-shown") >= 0 || Boolean(output && output.id && answered[output.id.of]);
    var failed = String(mark || "").indexOf("basis-mark-failed") >= 0;
    return "basis-failed-body" + (shown ? " basis-shown" : "") + (failed && !shown ? "" : " basis-failed-body-hidden");
  };
  basis.recovered = function () {
    var context = window.dash_clientside.callback_context;
    var output = context && context.outputs_list;
    if (output && output.id) answered[output.id.of] = true;
    var grids = window.dash_ag_grid;
    if (output && output.id && output.id.prop === "rowData" && grids && grids.getApiAsync) {
      /* a grid's answer, its first among them: from here an empty grid says so, where before it had nothing to say */
      grids.getApiAsync(output.id.of).then(
        function (api) {
          api.setGridOption("suppressNoRowsOverlay", false);
          if (!api.getDisplayedRowCount() && api.showNoRowsOverlay) api.showNoRowsOverlay();
          /* and its totals and count, once it holds the rows: an answer can land before the grid's listeners do */
          setTimeout(function () {
            totals({ api: api, type: "answered" });
          }, 0);
        },
        function () {
          /* the grid is gone */
        },
      );
    }
    return MARK_HIDDEN;
  };

  basis.totalsOf = totalsOf;
  basis.format = format;
  var grid = (window.dashAgGridFunctions = window.dashAgGridFunctions || {});
  grid.basisTotals = totals;
  grid.basisFormat = format;
  grid.basisScale = scale;
})();
