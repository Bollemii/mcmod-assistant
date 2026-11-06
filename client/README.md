# Client : module ChatTriggers `assist`

Ajoute `/assist <question>` dans Minecraft. La question part vers le serveur assistant
([server/](../server/README.md)) et la réponse s'affiche dans ton chat, visible de toi seul.

## Prérequis (Minecraft 26.2)

| Élément | Version | Où le trouver |
|---|---|---|
| Java | **25** | Fourni par le launcher officiel pour 26.2 |
| Fabric Loader | 0.19 ou plus | [fabricmc.net](https://fabricmc.net/use/installer/) |
| Fabric API | pour 26.2 | [Modrinth](https://modrinth.com/mod/fabric-api) |
| Fabric Language Kotlin | pour 26.2 | [Modrinth](https://modrinth.com/mod/fabric-language-kotlin) (requis par ChatTriggers) |
| ChatTriggers (`ctjs`) | **26.2v1** (bêta) | [Modrinth](https://modrinth.com/mod/ctjs) |

> Le launcher Excalia utilise déjà Fabric : il suffit d'y ajouter ChatTriggers
> (`ctjs`). Le module est validé par le staff d'Excalia.

## Installation du module

1. Lance le jeu une fois avec ChatTriggers pour qu'il crée ses dossiers.
2. Copie le dossier `assist/` dans `.minecraft/config/ChatTriggers/modules/`
   (la commande `/ct files` ouvre ce dossier). Le dossier doit s'appeler **`assist`**.
3. En jeu : `/ct load`.

## Configuration

```
/assistconfig url http://127.0.0.1:8000
/assistconfig token <ton jeton>
/assistconfig show
```

Le jeton est fourni par la personne qui fait tourner le serveur assistant
(`python -m api.tokens new "Ton pseudo"`). Il est enregistré dans
`modules/assist/config.json` : ne partage pas ce fichier.

## Utilisation

```
/assist Comment craft un four ?
```

- Un message gris confirme l'envoi. La réponse arrive quelques secondes plus tard,
  sans figer le jeu.
- Une seule question à la fois. Au-delà de 10 questions par minute, l'API demande
  d'attendre.
- Les pages du wiki ou les notes de patch utilisées s'affichent sous la réponse,
  cliquables. Si l'assistant ne sait pas, il renvoie vers le wiki du serveur.

### Mettre à jour le wiki

```
/assist maj
```

Relance la mise à jour du wiki et des notes de patch du serveur, puis affiche le bilan
(pages nouvelles, modifiées, supprimées). Le wiki est de toute façon mis à jour
automatiquement chaque semaine. Une même source n'est mise à jour qu'une fois par heure
au plus, tous joueurs confondus : sinon, la commande indique la date de la dernière
mise à jour.

Avec un jeton **admin** :

| Commande | Effet |
|---|---|
| `/assist maj force` | mise à jour complète, sans délai minimal |
| `/assist sources` | nombre de pages, date de la dernière et de la prochaine mise à jour |

## Tests hors du jeu

La logique du module (commandes, configuration, codes d'erreur) se teste avec Node,
grâce à des doublures des API ChatTriggers et Java :

```bash
node client/tests/run_module.js
```

Ces tests ne remplacent pas un essai en jeu : l'appel HTTP réel, l'affichage et
`Client.scheduleTask` ne s'exécutent que dans Minecraft.
