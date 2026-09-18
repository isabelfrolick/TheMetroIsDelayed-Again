/* Montréal métro delay tracker: renders the JSON produced by pipeline/process.py */
(() => {
  "use strict";

  const LINE_NUMBER = { green: 1, orange: 2, yellow: 4, blue: 5 };
  const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const CAUSES_EN = {
    "Clientèle": "customers",
    "Matériel roulant": "rolling stock",
    "Équipements fixes": "fixed equipment",
    "Exploitation trains": "train operations",
    "Exploitation stations": "station operations",
    "Autres": "other causes",
    "Non précisée": "unspecified",
  };

  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtInt = new Intl.NumberFormat("en-CA");
  const fmtMonth = (ym) => new Date(`${ym}-15T12:00:00`).toLocaleDateString("en-CA", { month: "long", year: "numeric" });
  const fmtDate = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString("en-CA", { month: "long", day: "numeric", year: "numeric" });
  const cause = (c) => CAUSES_EN[c] || c || "unspecified";
  const pct = (p) => (p == null ? "–" : `${Math.round(p * 100)}%`);
  const hourLabel = (h) => `${h}:00`;

  // Size an inline <select> to its chosen option, so the sentence reads without gaps
  const probe = document.createElement("span");
  probe.className = "select-probe";
  probe.setAttribute("aria-hidden", "true");
  function fitSelect(sel) {
    if (!probe.isConnected) document.body.appendChild(probe);
    const cs = getComputedStyle(sel);
    for (const prop of ["fontFamily", "fontSize", "fontWeight", "fontStretch", "fontVariationSettings", "letterSpacing"]) {
      probe.style[prop] = cs[prop];
    }
    probe.textContent = sel.options[sel.selectedIndex]?.text ?? "";
    const pad = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
    sel.style.width = `${Math.ceil(probe.getBoundingClientRect().width + pad + 6)}px`;
  }
  const fitAll = () => document.querySelectorAll(".inline-pick").forEach(fitSelect);

  const pastille = (key) =>
    `<span class="pastille" data-line="${key}" style="--c: var(--${key})" aria-hidden="true">${LINE_NUMBER[key] ?? ""}</span>`;

  async function getJSON(...paths) {
    for (const p of paths) {
      try {
        const r = await fetch(p, { cache: "no-cache" });
        if (r.ok) return await r.json();
      } catch (_) { /* try next path */ }
    }
    throw new Error(`Couldn't load ${paths[0]}`);
  }

  async function main() {
    let stats, lik, meta, geo;
    try {
      [stats, lik, meta] = await Promise.all([
        getJSON("data/stats.json"), getJSON("data/likelihood.json"), getJSON("data/meta.json"),
      ]);
      // On GitHub Pages the workflow copies the geometry into site/data; locally it lives in data/geo
      geo = await getJSON("data/metro_lines.geojson", "../data/geo/metro_lines.geojson").catch(() => null);
    } catch (err) {
      $("#freshness").outerHTML =
        `<p class="load-error">${esc(err.message)}. If you opened this file straight from disk, serve the folder instead: ` +
        `run <code>python -m http.server</code> in the repository and open <code>localhost:8000/site/</code>.</p>`;
      return;
    }

    document.querySelectorAll("[data-min-delay]").forEach((el) => (el.textContent = stats.min_delay_min));
    $("#freshness").textContent =
      `Incidents through ${fmtMonth(meta.data_through || stats.latest_month)}, ` +
      `refreshed ${fmtDate(meta.generated_at.slice(0, 10))}. The STM publishes its log a few months behind.`;

    const odds = setupOdds(stats, lik);
    const month = setupMonth(stats);
    const map = setupMap(stats, geo);
    month.onChange((m) => map.highlightStations(m.top3_stations));
    month.select(stats.latest_month);
    odds.render();
    fitAll();
    document.querySelectorAll(".inline-pick").forEach((s) => s.addEventListener("change", () => fitSelect(s)));
    document.fonts?.ready.then(fitAll);
    addEventListener("resize", fitAll);
  }

  /* ---- Likelihood sentence + heatmap -------------------------------------- */
  function setupOdds(stats, lik) {
    const daySel = $("#pick-day"), hourSel = $("#pick-hour"), lineSel = $("#pick-line");
    const hours = lik.hours;

    DAY_NAMES.forEach((d, i) => daySel.add(new Option(d, i)));
    hours.forEach((h, i) => hourSel.add(new Option(`${hourLabel(h)} and ${hourLabel((h + 1) % 24)}`, i)));
    lineSel.add(new Option("any line", "any"));
    Object.entries(stats.lines).forEach(([k, l]) => lineSel.add(new Option(`the ${l.label.toLowerCase()}`, k)));

    // Default to "right now" in Montréal, if the métro is running
    const now = new Date(new Date().toLocaleString("en-US", { timeZone: "America/Toronto" }));
    const hi = hours.indexOf(now.getHours());
    daySel.value = String((now.getDay() + 6) % 7);
    hourSel.value = String(hi >= 0 ? hi : hours.indexOf(8));

    const grid = $("#heatmap");
    grid.style.setProperty("--hours", hours.length);
    const cells = [];
    grid.insertAdjacentHTML("beforeend", `<span></span>` + hours.map((h) =>
      `<span class="hm-label hm-hour">${h % 3 === 0 ? hourLabel(h) : ""}</span>`).join(""));
    lik.weekdays.forEach((wd, d) => {
      grid.insertAdjacentHTML("beforeend", `<span class="hm-label">${wd}</span>`);
      cells[d] = hours.map((h, i) => {
        const b = document.createElement("button");
        b.type = "button";
        b.addEventListener("click", () => { daySel.value = d; hourSel.value = i; render(); fitSelect(daySel); fitSelect(hourSel); });
        grid.appendChild(b);
        return b;
      });
    });
    $("#window-note").textContent =
      `Based on ${fmtDate(lik.window.start)} to ${fmtDate(lik.window.end)}.`;

    function render() {
      const line = lineSel.value, d = +daySel.value, i = +hourSel.value;
      const g = line === "any" ? lik.any : lik.lines[line];
      const accent = line === "any" ? "var(--ink)" : `var(--${line})`;
      document.documentElement.style.setProperty("--accent", accent);
      document.documentElement.style.setProperty("--accent-text", line === "any" ? "var(--ink)" : `var(--${line}-text)`);

      const all = g.flat().filter((v) => v != null);
      const max = Math.max(...all, 0.01);
      const avg = all.reduce((a, b) => a + b, 0) / all.length;

      cells.forEach((row, dd) => row.forEach((b, ii) => {
        const v = g[dd][ii];
        b.style.setProperty("--v", v == null ? 0 : (0.08 + 0.92 * v / max).toFixed(3));
        b.setAttribute("aria-selected", dd === d && ii === i ? "true" : "false");
        b.setAttribute("aria-label", `${DAY_NAMES[dd]} ${hourLabel(hours[ii])}: ${pct(v)}`);
      }));

      const v = g[d][i];
      $("#odds-figure").textContent = pct(v);
      const ratio = avg > 0 && v != null ? v / avg : null;
      const lineName = line === "any" ? "the network" : `the ${stats.lines[line].label.toLowerCase()}`;
      let ctx = `The average hour on ${lineName} comes in at ${pct(avg)}.`;
      if (ratio != null && ratio >= 1.5) ctx += ` This hour is ${ratio.toFixed(1)} times as likely to be disrupted.`;
      else if (ratio != null && ratio <= 0.67) ctx += " This is one of the calmer hours.";
      $("#odds-context").textContent = ctx;
    }

    [daySel, hourSel, lineSel].forEach((s) => s.addEventListener("change", render));
    return { render };
  }

  /* ---- Month or year in review --------------------------------------------- */
  function setupMonth(stats) {
    const monthSel = $("#pick-month"), yearSel = $("#pick-year");
    const byMonth = new Map(stats.monthly.map((m) => [m.month, m]));
    const byYear = new Map(stats.yearly.map((y) => [y.year, y]));
    const monthName = (ym) => new Date(`${ym}-15T12:00:00`).toLocaleDateString("en-CA", { month: "long" });
    [...stats.yearly].reverse().forEach((y) => yearSel.add(new Option(y.year, y.year)));
    const listeners = [];

    function fillMonths(year, keep) {
      const months = byYear.get(year)?.months ?? [];
      monthSel.innerHTML = "";
      monthSel.add(new Option("all of", "all"));
      months.forEach((ym) => monthSel.add(new Option(monthName(ym), ym.slice(5))));
      monthSel.value = keep && months.includes(`${year}-${keep}`) ? keep : "all";
    }

    function render() {
      const year = yearSel.value, mm = monthSel.value;
      const isYear = mm === "all";
      const p = isYear ? byYear.get(year) : byMonth.get(`${year}-${mm}`);
      if (!p) return;

      const note = $("#period-note");
      const partial = isYear && p.months.length < 12;
      note.hidden = !partial;
      if (partial) {
        note.textContent = `${monthName(p.months[0])} to ${monthName(p.months.at(-1))} only: ` +
          "the STM hasn't published the rest of the year yet.";
      }

      $("#rank-lines").innerHTML = p.top3.map((k) => {
        const l = p.lines[k];
        return `<li>${pastille(k)}<span class="rank-name">${esc(stats.lines[k].label)}</span>
          <span class="rank-detail">${fmtInt.format(l.total_delay_min)} minutes over ${fmtInt.format(l.incidents)} delay${l.incidents === 1 ? "" : "s"}, ${l.avg_delay_min ?? "–"} min on average</span></li>`;
      }).join("");

      $("#rank-stations").innerHTML = p.top3_stations.length
        ? p.top3_stations.map((s) => `<li><span class="station-lines">${s.lines.map(pastille).join("")}</span>
            <span class="rank-name">${esc(s.name)}</span>
            <span class="rank-detail">${fmtInt.format(s.delays)} delay${s.delays === 1 ? "" : "s"} totalling ${fmtInt.format(s.delay_min)} minutes</span></li>`).join("")
        : `<li class="empty">No delays were recorded at a station in this period.</li>`;

      const g = stats.global;
      const diff = p.avg_delay_min != null && g.avg_delay_min ? p.avg_delay_min - g.avg_delay_min : null;
      const vs = diff == null ? "" : Math.abs(diff) < 0.5
        ? "In line with the 24-month average"
        : `${Math.abs(diff).toFixed(1)} min ${diff > 0 ? "longer" : "shorter"} than the 24-month average of ${g.avg_delay_min} min`;
      const count = isYear
        ? `About ${fmtInt.format(Math.round(p.incidents / p.months.length))} a month`
        : `${fmtInt.format(g.incidents)} over 24 months`;
      $("#network").innerHTML = `
        <div><dt>Average delay</dt><dd>${p.avg_delay_min ?? "–"} min<small>${vs}</small></dd></div>
        <div><dt>Delays recorded</dt><dd>${fmtInt.format(p.incidents)}<small>${count}</small></dd></div>
        <div><dt>Service hours with a delay somewhere</dt><dd>${(+g.pct_hours_delayed_any_line).toFixed(1)}%<small>Over 24 months, any line</small></dd></div>`;

      listeners.forEach((fn) => fn(p));
    }

    monthSel.addEventListener("change", render);
    yearSel.addEventListener("change", () => {
      fillMonths(yearSel.value, monthSel.value === "all" ? null : monthSel.value);
      render();
      fitSelect(monthSel);
    });
    return {
      select(ym) { yearSel.value = ym.slice(0, 4); fillMonths(ym.slice(0, 4), ym.slice(5)); render(); },
      onChange(fn) { listeners.push(fn); },
    };
  }

  /* ---- Map ------------------------------------------------------------------ */
  function setupMap(stats, geo) {
    const cards = $("#line-cards");
    const lineLayers = {};
    const cardEls = {};

    Object.entries(stats.lines).forEach(([k, l]) => {
      const li = document.createElement("li");
      li.className = "line-card";
      li.style.setProperty("--c", `var(--${k})`);
      li.innerHTML = `${pastille(k)}<h3>${esc(l.label)}</h3>
        <dl>
          <div><dt>Average delay</dt><dd>${l.avg_delay_min ?? "–"} min</dd></div>
          <div><dt>Hours with a delay</dt><dd>${(+l.pct_hours_delayed).toFixed(1)}%</dd></div>
          <div><dt>Delays</dt><dd>${fmtInt.format(l.incidents)}</dd></div>
        </dl>
        ${l.worst ? `<p class="worst">Worst: ${l.worst.minutes} min on ${fmtDate(l.worst.date)}, ${esc(cause(l.worst.cause))}.</p>` : ""}`;
      li.addEventListener("mouseenter", () => setActive(k));
      li.addEventListener("mouseleave", () => setActive(null));
      cards.appendChild(li);
      cardEls[k] = li;
    });

    if (!geo || typeof L === "undefined") {
      $("#map").innerHTML = `<p class="load-error">The line map couldn't load. The figures beside it are still current.</p>`;
      return { highlightStations() {} };
    }

    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    const map = L.map("map", { scrollWheelZoom: false, zoomSnap: 0.25 });
    L.tileLayer(`https://{s}.basemaps.cartocdn.com/${dark ? "dark_nolabels" : "light_nolabels"}/{z}/{x}/{y}{r}.png`, {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/">CARTO</a>',
      subdomains: "abcd", maxZoom: 18,
    }).addTo(map);

    const css = getComputedStyle(document.documentElement);
    const color = (k) => css.getPropertyValue(`--${k}`).trim();
    const lineTip = (k) => {
      const l = stats.lines[k];
      return `<strong>${esc(l.label)}</strong>
        <div class="tip-row"><span>Average delay</span><span>${l.avg_delay_min ?? "–"} min</span></div>
        <div class="tip-row"><span>Worst delay</span><span>${l.worst ? l.worst.minutes + " min" : "–"}</span></div>
        <div class="tip-row"><span>Service hours with a delay</span><span>${(+l.pct_hours_delayed).toFixed(1)}%</span></div>
        <div class="tip-row"><span>Delays, 24 months</span><span>${fmtInt.format(l.incidents)}</span></div>
        ${l.worst ? `<div class="tip-note">Worst on ${fmtDate(l.worst.date)} at ${esc(l.worst.start)}, ${esc(cause(l.worst.cause))}.</div>` : ""}
        ${l.top_cause ? `<div class="tip-note">Most common cause: ${esc(cause(l.top_cause.name))} (${l.top_cause.share_pct}%).</div>` : ""}`;
    };

    const bounds = L.latLngBounds([]);
    geo.features.filter((f) => f.properties.kind === "line").forEach((f) => {
      const k = f.properties.line;
      if (!stats.lines[k]) return;
      const latlngs = f.geometry.coordinates.map(([x, y]) => [y, x]);
      const visible = L.polyline(latlngs, { color: color(k), weight: 6, opacity: 0.95, lineCap: "round", lineJoin: "round", interactive: false }).addTo(map);
      // wide invisible twin so the line is easy to hover or tap
      const hit = L.polyline(latlngs, { color: "#000", weight: 22, opacity: 0 }).addTo(map);
      hit.bindTooltip(lineTip(k), { sticky: true, className: "delay-tip", direction: "top", offset: [0, -8] });
      hit.on("mouseover", () => setActive(k));
      hit.on("mouseout", () => setActive(null));
      lineLayers[k] = visible;
      bounds.extend(visible.getBounds());
    });

    const stationsByKey = new Map(stats.stations.map((s) => [s.key, s]));
    const maxDelays = Math.max(...stats.stations.map((s) => s.delays), 1);
    const ringLayer = L.layerGroup().addTo(map);
    stats.stations.forEach((s) => {
      if (!s.coords) return;
      const r = 3 + 9 * Math.sqrt(s.delays / maxDelays);
      L.circleMarker([s.coords[1], s.coords[0]], {
        radius: r, weight: 2, color: css.getPropertyValue("--ink").trim(),
        fillColor: css.getPropertyValue("--surface").trim(), fillOpacity: 1,
      }).bindTooltip(`<strong>${esc(s.name)}</strong>
          <div class="tip-row"><span>Delays, 24 months</span><span>${fmtInt.format(s.delays)}</span></div>
          <div class="tip-row"><span>Minutes of delay</span><span>${fmtInt.format(s.delay_min)}</span></div>
          <div class="tip-row"><span>All incidents recorded</span><span>${fmtInt.format(s.incidents)}</span></div>`,
        { className: "delay-tip", direction: "top", offset: [0, -r] }).addTo(map);
    });

    if (bounds.isValid()) map.fitBounds(bounds, { padding: [24, 24] });
    new ResizeObserver(() => map.invalidateSize()).observe($("#map"));

    function setActive(k) {
      Object.entries(lineLayers).forEach(([key, layer]) =>
        layer.setStyle({ weight: key === k ? 10 : 6, opacity: !k || key === k ? 0.95 : 0.35 }));
      Object.entries(cardEls).forEach(([key, el]) => el.classList.toggle("is-active", key === k));
    }

    return {
      highlightStations(top3) {
        ringLayer.clearLayers();
        top3.forEach((t, i) => {
          const s = stationsByKey.get(t.key) || t;
          if (!s.coords) return;
          L.circleMarker([s.coords[1], s.coords[0]], {
            radius: 17 - i * 2, weight: 3, color: css.getPropertyValue("--ink").trim(),
            fill: false, dashArray: i === 0 ? null : "4 4", interactive: false,
          }).addTo(ringLayer);
        });
      },
    };
  }

  main();
})();