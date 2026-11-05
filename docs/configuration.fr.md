# Configurer Sellsy et connecter le MCP

Ce guide concerne une instance hébergée pour votre propre entreprise. Chaque
collaborateur se connecte avec son compte Sellsy et conserve ses droits.
[English guide](configuration.md).

## 1. Préparer les trois URL

Choisissez le domaine HTTPS de votre serveur, par exemple `mcp.example.com`.

| Usage | Exemple à adapter |
|---|---|
| Origine publique dans `.env` | `SELLSY_MCP_PUBLIC_BASE_URL=https://mcp.example.com` |
| URL de redirection à enregistrer dans Sellsy | `https://mcp.example.com/oauth/callback` |
| URL MCP à ajouter dans votre assistant IA | `https://mcp.example.com/mcp` |

L'URL de redirection Sellsy pointe vers votre serveur MCP. Elle est distincte
du callback de ChatGPT ou de votre autre client IA, géré par le parcours OAuth
MCP. Votre proxy ou tunnel doit transmettre tout le domaine, y compris les
routes OAuth et `/.well-known/` : voir le [guide de déploiement](deployment.md).

## 2. Créer l'accès API dans Sellsy

1. Ouvrez votre compte Sellsy avec les droits de gestion des accès API.
2. Allez dans **Réglages → Portail Développeur → API v2**. Le
   [portail API v2](https://www.sellsy.fr/developer/api-v2) est aussi accessible
   directement après connexion.
3. Créez un accès avec un nom reconnaissable, par exemple `MCP — Mon entreprise`,
   et choisissez le type **Privé** : c'est le parcours prévu par ce serveur.
4. Renseignez votre URL complète `https://mcp.example.com/oauth/callback` dans
   le champ de redirection, en remplaçant le domaine. Sélectionnez les droits
   ci-dessous puis enregistrez.
5. Copiez le **Client ID** et le **Client Secret** dans votre `.env` privé.
   Conservez le secret dans votre coffre de mots de passe dès son affichage.

Le type **Personnel** correspond à un accès serveur à serveur ; ce MCP utilise
le type **Privé** pour connecter les collaborateurs. Le type **Public** est
soumis à la validation Sellsy pour une application ouverte à d'autres comptes.
Voir les [types d'accès expliqués par Sellsy](https://help.sellsy.com/fr/articles/5876615-types-d-acces-api).

Le code du MCP gère le flux authorization code, PKCE et le renouvellement des
jetons. Vous n'avez pas à fabriquer un bearer token ni à utiliser Postman pour
l'installation. Référence : [authentification API officielle](https://docs.sellsy.com/api/v2/#section/Authentication).

## 3. Cocher les autorisations Sellsy

Pour disposer de toutes les fonctions de lecture prévues par le dépôt,
sélectionnez les scopes suivants. Une sélection réduite limite les outils
utilisables ; `staffs.read` permet d'identifier le collaborateur connecté.

| Fonction | Scopes de lecture |
|---|---|
| Identité | `staffs.read` |
| Sociétés et contacts | `companies.read`, `contacts.read` |
| Catalogue | `items.read` |
| Devis et factures | `estimates.read`, `invoices.read` |
| Avoirs, commandes et livraisons | `credit-notes.read`, `orders.read`, `deliveries.read` |
| Opportunités | `opportunities.read` |
| Abonnements | `subscriptions.read` |
| Commentaires, fichiers et paiements | `comments.read`, `files.read`, `payments.read` |
| Recherche textuelle native | `search.read` |
| Taxes et métadonnées liées | `taxes.read`, `custom-fields.read`, `smart-tags.read` |

Les seules écritures optionnelles de cette intégration sont `estimates.write`
pour les devis et `opportunities.write` pour les opportunités. N'ajoutez pas
d'autres scopes d'écriture ni l'accès à l'API v1 pour ce MCP. Les droits et les
restrictions de licence du collaborateur continuent de s'appliquer.

La variable `SELLSY_MCP_SELLSY_SCOPES` définit les scopes que le serveur peut
demander : alignez-la avec votre sélection. Le fichier `.env.example` contient
les lectures et ces deux écritures optionnelles. Tant que
`SELLSY_MCP_ENABLE_WRITES=false`, les nouvelles autorisations MCP ne demandent
que la lecture à Sellsy.

## 4. Renseigner le serveur

Depuis la racine du dépôt :

```sh
cp .env.example .env
chmod 600 .env
```

Éditez les valeurs suivantes en conservant les autres réglages de l'exemple :

```dotenv
SELLSY_CLIENT_ID=VOTRE-CLIENT-ID-SELLSY
SELLSY_CLIENT_SECRET=VOTRE-CLIENT-SECRET-SELLSY
SELLSY_MCP_PUBLIC_BASE_URL=https://mcp.example.com
SELLSY_MCP_ENABLE_WRITES=false
```

Le Client ID doit être l'UUID seul, sans le libellé « Client ID : ». Pour le
secret aussi, copiez uniquement la valeur, sans préfixe ni espaces ajoutés.
Gardez `.env` hors Git et ne transmettez pas ces valeurs dans une conversation
IA. Il n'y a pas d'identifiant partagé fourni par le projet.

Installez les dépendances avec `uv sync --frozen --no-dev`, puis démarrez avec
`uv run --frozen --no-dev sellsy-mcp`, ou suivez le
[déploiement Docker/systemd](deployment.md). Redémarrez le service après un
changement de configuration.

## 5. Connecter chaque collaborateur

Dans un client MCP distant compatible Streamable HTTP et OAuth, ajoutez votre
URL `https://mcp.example.com/mcp`. Terminez la connexion dans le navigateur,
avec le compte Sellsy du collaborateur, puis acceptez les droits demandés.

Depuis cette même connexion, appelez `sellsy_get_capabilities` : vérifiez le
compte identifié, `upstream: reachable` et l'absence de scopes manquants
inattendus dans `missing_scopes`. Essayez ensuite une lecture, par exemple la
recherche d'une société connue. `/health/ready` vérifie le serveur et son
stockage ; ce seul résultat ne prouve pas que l'accès Sellsy fonctionne.

## 6. Activer les écritures si nécessaire

Trois conditions doivent être réunies :

1. Dans Sellsy, l'accès autorise `estimates.write` et/ou `opportunities.write`.
2. Le serveur les inclut dans `SELLSY_MCP_SELLSY_SCOPES`, avec
   `SELLSY_MCP_ENABLE_WRITES=true`, puis a été redémarré.
3. Le collaborateur renouvelle son consentement ; sa session MCP demande
   `sellsy.write`, en plus de `sellsy.read` et `offline_access` pour le
   renouvellement des jetons MCP.

Modifier `.env` ou les cases dans Sellsy ne met pas à jour les droits d'une
connexion déjà ouverte. Reconnectez le client et contrôlez les droits effectifs.
L'option globale `writes_enabled` ne prouve pas à elle seule que la session
peut écrire. La procédure reste : préparer, relire le diff, approuver
explicitement, appliquer, puis vérifier la relecture. Voir aussi les
[deux couches de permissions](configuration.md#two-independent-permission-layers).

## Dépannage

| Symptôme | À vérifier |
|---|---|
| Client ID invalide ou `Client authentication failed` | Accès API v2 privé, UUID seul, valeur du secret sans libellé, redémarrage après modification de `.env` |
| Erreur de redirection | Domaine HTTPS exact, chemin `/oauth/callback`, absence de slash final ajouté, routage du proxy |
| Identité introuvable | `staffs.read` accordé et compte collaborateur actif |
| Scope manquant ou HTTP 403 | Scopes sélectionnés dans Sellsy, consentement actuel, droits du collaborateur et licence |
| Lecture possible mais écriture refusée | Interrupteur serveur, scope MCP `sellsy.write`, scope Sellsy de l'objet et nouveau consentement |
| `/mcp` renvoie 401 sans connexion | Comportement attendu ; passer par la connexion OAuth du client |

N'élargissez pas les droits en modifiant la base des jetons. Le guide de
[configuration et administration](configuration.md) décrit le renouvellement
ciblé d'un consentement existant.
