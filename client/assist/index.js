// Module ChatTriggers « assist » (ctjs 26.2v1, Fabric, Minecraft 26.2).
//
//   /assist <question>             pose une question à l'assistant
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

function linesForResponse(status, body, response) {
    let data = {};
    try {
        data = JSON.parse(body);
    } catch (e) {
        // corps vide ou non JSON : on s'en tient au code HTTP
    }
    switch (status) {
        case 200:
            return answerLines(data);
        case 401:
            return [line("&cJeton refusé. Configure-le avec &e/assistconfig token <jeton>&c.")];
        case 422:
            return [line("&cQuestion invalide (" + MAX_QUESTION + " caractères au plus).")];
        case 429: {
            const wait = response.headers().firstValue("Retry-After").orElse("?");
            return [line("&eTrop de questions d'affilée, réessaie dans " + wait + " s.")];
        }
        default:
            return [line("&cL'assistant est indisponible pour le moment (code " + status + ").")];
    }
}

function ask(question) {
    if (!config.token) {
        say("&cAucun jeton configuré. Utilise &e/assistconfig token <jeton>&c.");
        return;
    }
    if (pending) {
        say("&7Une question est déjà en cours, patiente un instant…");
        return;
    }
    if (question.length > MAX_QUESTION) {
        say("&cQuestion trop longue (" + MAX_QUESTION + " caractères au plus).");
        return;
    }

    pending = true;
    say("&7&o" + question + " &8— je cherche…");
    const body = JSON.stringify({
        question: question,
        server: Server.getIP() || null,
        mc_version: minecraftVersion(),
        lang: "fr",
    });
    const url = config.apiUrl.replace(/\/+$/, "") + "/v1/ask";
    const token = config.token;

    new Thread(() => {
        let lines;
        try {
            const request = HttpRequest.newBuilder(URI.create(url))
                .timeout(Duration.ofSeconds(30))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + token)
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();
            const response = http.send(request, HttpResponse.BodyHandlers.ofString());
            lines = linesForResponse(response.statusCode(), String(response.body()), response);
        } catch (e) {
            print("[assist] échec de la requête vers " + url + " : " + e);
            lines = [line("&cServeur de l'assistant injoignable (" + config.apiUrl + ").")];
        } finally {
            pending = false;
        }
        sayLater(lines);
    }).start();
}

// --- Commandes ----------------------------------------------------------------------

register("command", (...args) => {
    const question = args.join(" ").trim();
    if (!question) {
        say("&fPose ta question : &e/assist Comment craft un four ?");
        return;
    }
    ask(question);
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
