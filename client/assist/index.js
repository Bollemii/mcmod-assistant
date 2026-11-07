// Module ChatTriggers « assist » (ctjs 26.2v1, Fabric, Minecraft 26.2).
//
//   /assist <question>             pose une question à l'assistant
//   /assist maj                    met à jour le wiki et les notes de patch du serveur
//   /assist maj force              (jeton admin) mise à jour complète, sans délai minimal
//   /assist sources                (jeton admin) état des sources et date de mise à jour
//   /assistconfig url <adresse>    adresse de l'API (défaut : http://127.0.0.1:8000)
//   /assistconfig token <jeton>    jeton personnel, fourni par l'administrateur de l'API
//   /assistconfig show             affiche la configuration
//
// La requête HTTP part dans un thread à part : le jeu ne se fige jamais pendant l'attente.
// L'affichage revient sur le thread du jeu via Client.scheduleTask.
// Contrat de l'API : docs/architecture.md, « Contrat de l'API ».

const HttpClient = Java.type("java.net.http.HttpClient");
const HttpRequest = Java.type("java.net.http.HttpRequest");
const HttpResponse = Java.type("java.net.http.HttpResponse");
const URI = Java.type("java.net.URI");
const Duration = Java.type("java.time.Duration");

const MODULE = "assist";            // nom du dossier du module (pour FileLib)
const CONFIG_FILE = "config.json";
const PREFIX = "&b[Assist] &r";
const MAX_QUESTION = 256;           // limite de l'API, comme le chat Minecraft
const ASK_TIMEOUT_S = 90;           // le mode allégé (LLM local) met 20 à 30 s, plus qu'avec Gemini
const DEFAULT_CONFIG = { apiUrl: "http://127.0.0.1:8000", token: "" };

const http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
let config = loadConfig();
let pending = false;                // une seule question à la fois

// --- Configuration ------------------------------------------------------------------

function loadConfig() {
    const raw = FileLib.read(MODULE, CONFIG_FILE);
    if (!raw) {
        saveConfig(DEFAULT_CONFIG);
        return Object.assign({}, DEFAULT_CONFIG);
    }
    try {
        return Object.assign({}, DEFAULT_CONFIG, JSON.parse(raw));
    } catch (e) {
        print("[assist] config.json illisible, valeurs par défaut utilisées : " + e);
        return Object.assign({}, DEFAULT_CONFIG);
    }
}

function saveConfig(c) {
    FileLib.write(MODULE, CONFIG_FILE, JSON.stringify(c, null, 2));
}

// --- Affichage ----------------------------------------------------------------------

function say(message) {
    ChatLib.chat(PREFIX + message);
}

// Affiche depuis n'importe quel thread : le chat n'est modifié que sur le thread du jeu.
function sayLater(lines) {
    Client.scheduleTask(0, () => lines.forEach(line => line.chat()));
}

function line(message) {
    return new TextComponent(PREFIX + message);
}

function answerLines(data) {
    // La réponse est passée en objet { text } pour ne pas interpréter d'éventuels « & ».
    const lines = [new TextComponent(PREFIX, { text: String(data.answer), color: "white" })];
    if (data.degraded) {
        // Gemini indisponible : réponse du LLM local, plus courte et moins complète.
        lines.push(line("&8&o(mode allégé : réponse moins complète, le wiki fait foi)"));
    }
    (data.sources || []).forEach(source => {
        lines.push(new TextComponent({
            text: "  ↳ " + source.title,
            color: "gray",
            underline: true,
            clickEvent: { action: "open_url", value: source.url },
            hoverEvent: { action: "show_text", value: source.url },
        }));
    });
    return lines;
}

// --- Appel à l'API ------------------------------------------------------------------

function minecraftVersion() {
    try {
        return String(Java.type("net.minecraft.SharedConstants").getCurrentVersion().name());
    } catch (e) {
        return null;    // l'API retombe sur la version déclarée pour le serveur
    }
}

// Messages communs à toutes les commandes ; null = code propre à la commande.
function commonError(status, data, response) {
    switch (status) {
        case 401:
            return [line("&cJeton refusé. Configure-le avec &e/assistconfig token <jeton>&c.")];
        case 403:
            return [line("&cCommande réservée aux jetons admin.")];
        case 404:
            return [line("&cCe serveur n'est pas encore connu de l'assistant.")];
        case 429: {
            const wait = response.headers().firstValue("Retry-After").orElse("?");
            return [line("&eTrop de demandes d'affilée, réessaie dans " + wait + " s.")];
        }
        default:
            return null;
    }
}

function unavailable(status, data) {
    const detail = data && data.error ? " " + data.error : "";
    return [line("&cL'assistant est indisponible pour le moment (code " + status + ")." + detail)];
}

function linesForResponse(status, body, response) {
    const data = parseJson(body);
    if (status === 200) return answerLines(data);
    if (status === 422) return [line("&cQuestion invalide (" + MAX_QUESTION + " caractères au plus).")];
    return commonError(status, data, response) || [line("&cL'assistant est indisponible pour le moment (code " + status + ").")];
}

function parseJson(body) {
    try {
        return JSON.parse(body);
    } catch (e) {
        return {};   // corps vide ou non JSON : on s'en tient au code HTTP
    }
}

// --- Mise à jour des sources ----------------------------------------------------------

const SOURCE_LABELS = { wiki: "Wiki", patch_notes: "Notes de patch" };

function label(key) {
    return SOURCE_LABELS[key] || key;
}

function two(n) {
    return (n < 10 ? "0" : "") + n;
}

function formatDate(iso) {
    if (!iso) return "jamais";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return two(d.getDate()) + "/" + two(d.getMonth() + 1) + " à " + two(d.getHours()) + "h" + two(d.getMinutes());
}

// plural(2, "nouvelle page", "nouvelles pages") -> "2 nouvelles pages"
function plural(n, one, many) {
    return n + " " + (n > 1 ? many || one.replace(/(\S+)/g, "$1s") : one);
}

function refreshLines(data) {
    return (data.sources || []).map(s => {
        const name = "&f" + label(s.source) + " : ";
        switch (s.status) {
            case "done": {
                const changes = (s.added || 0) + (s.changed || 0) + (s.removed || 0);
                if (!changes) return line(name + "&ajà jour, aucun changement.");
                return line(name + "&amis à jour &7(" + plural(s.added || 0, "nouvelle page") + ", "
                    + plural(s.changed || 0, "modifiée") + ", " + plural(s.removed || 0, "supprimée") + ").");
            }
            case "recent": {
                const minutes = Math.ceil((s.retry_after_s || 0) / 60);
                return line(name + "&edéjà mis à jour le " + formatDate(s.refreshed_at)
                    + "&7, nouvelle demande possible dans " + minutes + " min.");
            }
            case "running":
                return line(name + "&emise à jour en cours, les réponses en tiendront compte dès qu'elle sera finie.");
            default:
                return line(name + "&céchec de la mise à jour, l'ancienne version reste utilisée.");
        }
    });
}

function sourcesLines(data) {
    return (data.sources || []).map(s => line("&f" + label(s.source) + " : &7" + plural(s.documents || 0, "page")
        + ", " + plural(s.chunks || 0, "extrait") + ", mis à jour le " + formatDate(s.refreshed_at)
        + (s.next_refresh ? ", prochaine le " + formatDate(s.next_refresh) : ", pas de mise à jour automatique")
        + (s.running ? " &e(en cours)" : "")));
}

// --- Envoi des requêtes ---------------------------------------------------------------

// Envoie la requête dans un thread ; handle(status, data, response) renvoie les lignes à afficher.
function send(method, path, payload, timeoutS, handle) {
    const url = config.apiUrl.replace(/\/+$/, "") + path;
    const token = config.token;
    const body = payload ? JSON.stringify(payload) : null;
    pending = true;
    new Thread(() => {
        let lines;
        try {
            let builder = HttpRequest.newBuilder(URI.create(url))
                .timeout(Duration.ofSeconds(timeoutS))
                .header("Authorization", "Bearer " + token);
            builder = method === "POST"
                ? builder.header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(body))
                : builder.GET();
            const response = http.send(builder.build(), HttpResponse.BodyHandlers.ofString());
            lines = handle(response.statusCode(), String(response.body()), response);
        } catch (e) {
            print("[assist] échec de la requête vers " + url + " : " + e);
            lines = [line("&cServeur de l'assistant injoignable (" + config.apiUrl + ").")];
        } finally {
            pending = false;
        }
        sayLater(lines);
    }).start();
}

function ready() {
    if (!config.token) {
        say("&cAucun jeton configuré. Utilise &e/assistconfig token <jeton>&c.");
        return false;
    }
    if (pending) {
        say("&7Une demande est déjà en cours, patiente un instant…");
        return false;
    }
    return true;
}

function ask(question) {
    if (!ready()) return;
    if (question.length > MAX_QUESTION) {
        say("&cQuestion trop longue (" + MAX_QUESTION + " caractères au plus).");
        return;
    }
    say("&7&o" + question + " &8— je cherche…");
    send("POST", "/v1/ask", {
        question: question,
        server: Server.getIP() || null,
        mc_version: minecraftVersion(),
        lang: "fr",
    }, ASK_TIMEOUT_S, linesForResponse);
}

function refresh(force) {
    if (!ready()) return;
    say("&7Mise à jour " + (force ? "complète " : "") + "du wiki et des notes de patch…");
    send("POST", "/v1/refresh", { server: Server.getIP() || null, force: force }, 120, (status, body, response) => {
        const data = parseJson(body);
        if (status === 200) return refreshLines(data);
        return commonError(status, data, response) || unavailable(status, data);
    });
}

function showSources() {
    if (!ready()) return;
    const server = encodeURIComponent(Server.getIP() || "");
    send("GET", "/v1/sources?server=" + server, null, 15, (status, body, response) => {
        const data = parseJson(body);
        if (status === 200) return sourcesLines(data);
        return commonError(status, data, response) || unavailable(status, data);
    });
}

// --- Commandes ----------------------------------------------------------------------

register("command", (...args) => {
    const question = args.join(" ").trim();
    const command = question.toLowerCase();
    if (!question) {
        say("&fPose ta question : &e/assist Comment craft un four ? &7| &e/assist maj &7pour mettre à jour le wiki");
    } else if (command === "maj" || command === "maj force") {
        refresh(command === "maj force");
    } else if (command === "sources") {
        showSources();
    } else {
        ask(question);
    }
}).setName("assist");

register("command", (...args) => {
    const key = args[0];
    const value = args.slice(1).join(" ").trim();
    if (key === "url" && value) {
        config.apiUrl = value;
        saveConfig(config);
        say("&aAdresse de l'API : &f" + value);
    } else if (key === "token" && value) {
        config.token = value;
        saveConfig(config);
        say("&aJeton enregistré.");
    } else if (key === "show") {
        const masked = config.token ? config.token.slice(0, 8) + "…" : "&c(aucun)";
        say("&fAPI : &e" + config.apiUrl + " &f— jeton : &e" + masked);
    } else {
        say("&f/assistconfig url <adresse> &7| &f/assistconfig token <jeton> &7| &f/assistconfig show");
    }
}).setTabCompletions("url", "token", "show").setName("assistconfig");
