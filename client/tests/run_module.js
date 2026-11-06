// Exécute client/assist/index.js sous Node, avec des doublures des API ChatTriggers et
// Java, pour tester la logique du module hors du jeu (commandes, configuration, codes HTTP).
// Lancer : node client/tests/run_module.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SOURCE = fs.readFileSync(path.join(__dirname, "..", "assist", "index.js"), "utf8");

function load(scenario) {
    const files = {};                    // FileLib en mémoire
    const chat = [];                     // messages affichés
    const commands = {};
    const sent = [];                     // requêtes HTTP envoyées

    class TextComponent {
        constructor(...parts) { this.parts = parts; }
        text() { return this.parts.map(p => (typeof p === "string" ? p : p.text)).join(""); }
        chat() { chat.push(this.text()); return this; }
    }
    const javaTypes = {
        "java.net.http.HttpClient": {
            newBuilder: () => ({ connectTimeout() { return this; }, build: () => ({
                send: req => {
                    sent.push(req);
                    if (scenario.network === "down") throw new Error("Connection refused");
                    const r = scenario.response;
                    return {
                        statusCode: () => r.status,
                        body: () => JSON.stringify(r.body || {}),
                        headers: () => ({ firstValue: name => ({ orElse: d => (r.headers || {})[name] || d }) }),
                    };
                },
            }) }),
        },
        "java.net.http.HttpRequest": {
            newBuilder: uri => {
                const req = { uri, headers: {} };
                const b = {
                    timeout() { return b; },
                    header(k, v) { req.headers[k] = v; return b; },
                    POST(body) { req.method = "POST"; req.body = body; return b; },
                    GET() { req.method = "GET"; return b; },
                    build: () => req,
                };
                return b;
            },
            BodyPublishers: { ofString: s => s },
        },
        "java.net.http.HttpResponse": { BodyHandlers: { ofString: () => null } },
        "java.net.URI": { create: s => s },
        "java.time.Duration": { ofSeconds: s => s },
        "net.minecraft.SharedConstants": { getCurrentVersion: () => ({ name: () => "26.2" }) },
    };
    const ctx = {
        Java: { type: name => { if (!(name in javaTypes)) throw new Error("type inconnu " + name); return javaTypes[name]; } },
        FileLib: {
            read: (m, f) => files[m + "/" + f] || null,
            write: (m, f, s) => { files[m + "/" + f] = s; },
        },
        ChatLib: { chat: s => chat.push(s) },
        TextComponent,
        Client: { scheduleTask: (delay, fn) => fn() },
        Server: { getIP: () => scenario.ip || "play.excalia.fr" },
        Thread: class { constructor(fn) { this.fn = fn; } start() { this.fn(); } },
        register: (type, fn) => {
            const trigger = { setName(n) { commands[n] = fn; return trigger; }, setTabCompletions() { return trigger; } };
            return trigger;
        },
        print: () => {},
        JSON, Object, String,
    };
    if (scenario.config) files["assist/config.json"] = JSON.stringify(scenario.config);
    vm.runInNewContext(SOURCE, ctx);
    return { files, chat, commands, sent, run: (cmd, ...args) => commands[cmd](...args) };
}

const OK = { status: 200, body: { answer: "Huit pierres taillées autour d'une case vide.", query_id: "abc",
    sources: [{ title: "Four", url: "https://excalia.fr/wiki/four" }] } };
const tests = {
    "crée une configuration par défaut": () => {
        const m = load({});
        assert.deepStrictEqual(JSON.parse(m.files["assist/config.json"]), { apiUrl: "http://127.0.0.1:8000", token: "" });
    },
    "refuse de poser une question sans jeton": () => {
        const m = load({ response: OK });
        m.run("assist", "Comment", "craft", "un", "four", "?");
        assert.strictEqual(m.sent.length, 0);
        assert.match(m.chat.at(-1), /Aucun jeton/);
    },
    "configure l'URL et le jeton": () => {
        const m = load({});
        m.run("assistconfig", "url", "https://assist.example.org/");
        m.run("assistconfig", "token", "mca_secret123");
        assert.deepStrictEqual(JSON.parse(m.files["assist/config.json"]),
            { apiUrl: "https://assist.example.org/", token: "mca_secret123" });
        m.run("assistconfig", "show");
        assert.match(m.chat.at(-1), /mca_secr…/);
        assert.doesNotMatch(m.chat.at(-1), /secret123/);
    },
    "envoie la question et affiche la réponse et ses sources": () => {
        const m = load({ config: { apiUrl: "http://h:8000/", token: "t1" }, response: OK });
        m.run("assist", "Comment", "craft", "un", "four", "?");
        const req = m.sent[0];
        assert.strictEqual(req.uri, "http://h:8000/v1/ask");
        assert.strictEqual(req.headers.Authorization, "Bearer t1");
        assert.deepStrictEqual(JSON.parse(req.body),
            { question: "Comment craft un four ?", server: "play.excalia.fr", mc_version: "26.2", lang: "fr" });
        assert.match(m.chat.at(-2), /Huit pierres taillées/);
        assert.match(m.chat.at(-1), /↳ Four/);
    },
    "explique chaque code d'erreur": () => {
        const cases = [[401, /Jeton refusé/], [422, /Question invalide/], [429, /dans 42 s/], [503, /indisponible.*503/]];
        for (const [status, expected] of cases) {
            const m = load({ config: { token: "t" }, response: { status, headers: { "Retry-After": "42" } } });
            m.run("assist", "question");
            assert.match(m.chat.at(-1), expected, String(status));
        }
    },
    "signale un serveur injoignable puis accepte une nouvelle question": () => {
        const m = load({ config: { token: "t" }, network: "down" });
        m.run("assist", "question");
        assert.match(m.chat.at(-1), /injoignable/);
        m.run("assist", "encore");
        assert.strictEqual(m.sent.length, 2);  // « pending » a bien été remis à zéro
    },
    "/assist maj : envoie la demande et affiche le bilan par source": () => {
        const m = load({ config: { token: "t" }, response: { status: 200, body: { server: "excalia", sources: [
            { source: "wiki", status: "done", added: 2, changed: 1, removed: 0 },
            { source: "patch_notes", status: "recent", refreshed_at: "2026-11-06T09:30:00+00:00", retry_after_s: 1500 },
        ] } } });
        m.run("assist", "MAJ");
        const req = m.sent[0];
        assert.strictEqual(req.uri, "http://127.0.0.1:8000/v1/refresh");
        assert.deepStrictEqual(JSON.parse(req.body), { server: "play.excalia.fr", force: false });
        assert.match(m.chat.at(-2), /Wiki : .*mis à jour .*2 nouvelles pages, 1 modifiée, 0 supprimée/);
        assert.match(m.chat.at(-1), /Notes de patch : .*déjà mis à jour le 06\/11.*dans 25 min/);
    },
    "/assist maj force : demande forcée, refusée sans jeton admin": () => {
        const m = load({ config: { token: "t" }, response: { status: 403, body: { error: "réservé" } } });
        m.run("assist", "maj", "force");
        assert.deepStrictEqual(JSON.parse(m.sent[0].body), { server: "play.excalia.fr", force: true });
        assert.match(m.chat.at(-1), /réservée aux jetons admin/);
    },
    "/assist maj sans changement, en cours, en échec, sans base": () => {
        const m = load({ config: { token: "t" }, response: { status: 200, body: { sources: [
            { source: "wiki", status: "done", added: 0, changed: 0, removed: 0 },
            { source: "patch_notes", status: "running" }, { source: "autre", status: "error" }] } } });
        m.run("assist", "maj");
        assert.match(m.chat.at(-3), /aucun changement/);
        assert.match(m.chat.at(-2), /en cours/);
        assert.match(m.chat.at(-1), /autre : .*échec/);
        const down = load({ config: { token: "t" }, response: { status: 503, body: { error: "Pas de base." } } });
        down.run("assist", "maj");
        assert.match(down.chat.at(-1), /indisponible.*503.*Pas de base/);
    },
    "/assist sources : requête GET et état de chaque source": () => {
        const m = load({ config: { token: "t" }, response: { status: 200, body: { sources: [
            { source: "wiki", documents: 128, chunks: 818, refreshed_at: "2026-11-06T09:30:00+00:00",
              next_refresh: "2026-11-13T09:30:00+00:00", running: false }] } } });
        m.run("assist", "sources");
        assert.strictEqual(m.sent[0].method, "GET");
        assert.strictEqual(m.sent[0].uri, "http://127.0.0.1:8000/v1/sources?server=play.excalia.fr");
        assert.match(m.chat.at(-1), /Wiki : .*128 pages, 818 extraits, mis à jour le 06\/11.*prochaine le 13\/11/);
    },
    "une question qui commence par « maj » reste une question": () => {
        const m = load({ config: { token: "t" }, response: OK });
        m.run("assist", "maj", "de", "mon", "île", "?");
        assert.strictEqual(m.sent[0].uri, "http://127.0.0.1:8000/v1/ask");
    },
    "refuse une question vide ou trop longue": () => {
        const m = load({ config: { token: "t" }, response: OK });
        m.run("assist");
        m.run("assist", "a".repeat(300));
        assert.strictEqual(m.sent.length, 0);
        assert.match(m.chat.at(-1), /trop longue/);
    },
};

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
    try { fn(); console.log("ok   " + name); } catch (e) { failed++; console.log("ÉCHEC " + name + "\n     " + e.message); }
}
console.log(failed ? `${failed} échec(s)` : `${Object.keys(tests).length} tests OK`);
process.exit(failed ? 1 : 0);
