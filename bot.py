import os, json, re, time, asyncio, io
from collections import defaultdict, deque
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

# ====== CONFIG ======
TOKEN = os.getenv("DISCORD_TOKEN", "COLLE_TON_NOUVEAU_TOKEN_ICI")
GUILD_ID = 1562753209536094238
DATA_FILE = "data.json"

DEFAULT = {
    "unverified_role": None, "member_role": None, "log_channel": None,
    "hierarchy_channel": None, "hierarchy_message": None,
    "claim_roles": [], "warns": {},
    "ticket_category": None, "ticket_staff_role": None, "ticket_count": 0,
    "antiraid": {"enabled": True, "joins": 5, "seconds": 10, "min_age_days": 3,
                 "action": "kick", "spam_msgs": 6, "spam_seconds": 5,
                 "block_invites": True},
}


def load():
    data = json.loads(json.dumps(DEFAULT))
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            ar = saved.pop("antiraid", {})
            data.update(saved)
            data["antiraid"].update(ar)
        except Exception:
            pass
    return data


def save():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


db = load()
raid_until = 0.0
joins = deque()
msgs = defaultdict(lambda: deque())
INVITE_RE = re.compile(r"(discord\.gg|discord(?:app)?\.com/invite)/\w+", re.I)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class Bot(commands.Bot):
    async def setup_hook(self):
        self.add_view(ClaimOpenView())
        self.add_view(RulesView())
        self.add_view(TicketOpenView())
        self.add_view(TicketCloseView())
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)


bot = Bot(command_prefix="!", intents=intents)


# ====== OUTILS ======
async def log(guild, text):
    cid = db["log_channel"]
    ch = guild.get_channel(cid) if cid else None
    if ch:
        try:
            await ch.send(embed=discord.Embed(description=text, color=0x5865F2))
        except discord.HTTPException:
            pass


def can_act(inter, member):
    g = inter.guild
    if member.id == g.owner_id or member.id == inter.user.id:
        return False
    if inter.user.id != g.owner_id and inter.user.top_role <= member.top_role:
        return False
    return g.me.top_role > member.top_role


async def deny(inter):
    await inter.response.send_message(
        "❌ Impossible : rôle trop haut (toi ou moi) ou cible invalide.", ephemeral=True)


# ====== HIÉRARCHIE AUTO ======
def build_hierarchy(guild):
    roles = [r for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)
             if not r.is_default()]
    lines = [f"{r.mention} — `{len(r.members)}`" for r in roles]
    desc = "\n".join(lines) or "Aucun rôle."
    if len(desc) > 4000:
        desc = desc[:3990] + "\n…"
    e = discord.Embed(title=f"📜 Hiérarchie de {guild.name}", description=desc,
                      color=0x5865F2)
    e.set_footer(text=f"{len(roles)} rôles • mise à jour auto")
    return e


_hier_lock = asyncio.Lock()


async def update_hierarchy(guild):
    cid = db["hierarchy_channel"]
    ch = guild.get_channel(cid) if cid else None
    if not ch:
        return
    async with _hier_lock:
        embed = build_hierarchy(guild)
        mid = db["hierarchy_message"]
        if mid:
            try:
                msg = await ch.fetch_message(mid)
                await msg.edit(embed=embed)
                return
            except discord.NotFound:
                pass
        msg = await ch.send(embed=embed)
        db["hierarchy_message"] = msg.id
        save()


@bot.event
async def on_guild_role_create(role):
    await update_hierarchy(role.guild)


@bot.event
async def on_guild_role_delete(role):
    if role.id in db["claim_roles"]:
        db["claim_roles"].remove(role.id)
        save()
    await update_hierarchy(role.guild)


@bot.event
async def on_guild_role_update(before, after):
    await update_hierarchy(after.guild)


# ====== CLAIM ROLE ======
class ClaimButton(discord.ui.Button):
    def __init__(self, role, row):
        super().__init__(label=role.name[:80], style=discord.ButtonStyle.primary,
                         custom_id=f"claim_{role.id}_{time.time_ns()}", row=row)
        self.role_id = role.id

    async def callback(self, inter):
        role = inter.guild.get_role(self.role_id)
        if not role:
            return await inter.response.send_message("Rôle introuvable.", ephemeral=True)
        try:
            if role in inter.user.roles:
                await inter.user.remove_roles(role, reason="Claim role")
                msg = f"➖ Rôle {role.mention} retiré."
            else:
                await inter.user.add_roles(role, reason="Claim role")
                msg = f"✅ Rôle {role.mention} attribué !"
        except discord.Forbidden:
            msg = "❌ Je ne peux pas donner ce rôle (il est au-dessus du mien)."
        await inter.response.send_message(msg, ephemeral=True)


class ClaimPanelView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=300)
        roles = [guild.get_role(r) for r in db["claim_roles"]]
        for i, r in enumerate([r for r in roles if r][:25]):
            self.add_item(ClaimButton(r, i // 5))


class ClaimOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim your role", emoji="🎁",
                       style=discord.ButtonStyle.success, custom_id="claim_open")
    async def open(self, inter, button):
        roles = [inter.guild.get_role(r) for r in db["claim_roles"]]
        roles = [r for r in roles if r][:25]
        if not roles:
            return await inter.response.send_message(
                "Aucun rôle disponible pour l'instant.", ephemeral=True)
        e = discord.Embed(title="🎁 Rôles disponibles",
                          description="\n".join(f"• {r.mention}" for r in roles)
                          + "\n\nClique sur un bouton pour obtenir/retirer le rôle.",
                          color=0x57F287)
        await inter.response.send_message(embed=e, view=ClaimPanelView(inter.guild),
                                          ephemeral=True)


claim = app_commands.Group(name="claim", description="Gérer les rôles à claim",
                           default_permissions=discord.Permissions(manage_roles=True))


@claim.command(name="ajouter", description="Ajouter un rôle à la liste claim")
async def claim_add(inter: discord.Interaction, role: discord.Role):
    if role.id in db["claim_roles"]:
        return await inter.response.send_message("Déjà dans la liste.", ephemeral=True)
    if len(db["claim_roles"]) >= 25:
        return await inter.response.send_message("Max 25 rôles.", ephemeral=True)
    db["claim_roles"].append(role.id)
    save()
    await inter.response.send_message(f"✅ {role.mention} ajouté.", ephemeral=True)


@claim.command(name="retirer", description="Retirer un rôle de la liste claim")
async def claim_remove(inter: discord.Interaction, role: discord.Role):
    if role.id not in db["claim_roles"]:
        return await inter.response.send_message("Pas dans la liste.", ephemeral=True)
    db["claim_roles"].remove(role.id)
    save()
    await inter.response.send_message(f"🗑️ {role.mention} retiré.", ephemeral=True)


@claim.command(name="liste", description="Voir la liste des rôles claim")
async def claim_list(inter: discord.Interaction):
    roles = [inter.guild.get_role(r) for r in db["claim_roles"]]
    txt = "\n".join(r.mention for r in roles if r) or "Vide."
    await inter.response.send_message(txt, ephemeral=True)


@claim.command(name="panel", description="Envoyer le panel « Claim your role »")
async def claim_panel(inter: discord.Interaction, salon: discord.TextChannel = None):
    ch = salon or inter.channel
    e = discord.Embed(title="🎁 Claim your role",
                      description="Appuie sur le bouton pour choisir tes rôles !",
                      color=0x57F287)
    await ch.send(embed=e, view=ClaimOpenView())
    await inter.response.send_message("✅ Panel envoyé.", ephemeral=True)


bot.tree.add_command(claim)


# ====== UNVERIFIED / RÈGLEMENT ======
async def verify(member):
    g = member.guild
    mr = g.get_role(db["member_role"]) if db["member_role"] else None
    ur = g.get_role(db["unverified_role"]) if db["unverified_role"] else None
    if mr:
        await member.add_roles(mr, reason="Règlement accepté")
    if ur and ur in member.roles:
        await member.remove_roles(ur, reason="Règlement accepté")
    await log(g, f"✅ {member.mention} a accepté le règlement.")


class RulesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="J'accepte le règlement", emoji="✅",
                       style=discord.ButtonStyle.success, custom_id="rules_accept")
    async def accept(self, inter, button):
        if not db["member_role"]:
            return await inter.response.send_message(
                "Rôle member non configuré.", ephemeral=True)
        try:
            await verify(inter.user)
            await inter.response.send_message("✅ Bienvenue !", ephemeral=True)
        except discord.Forbidden:
            await inter.response.send_message(
                "❌ Je n'ai pas la permission (mon rôle doit être au-dessus).",
                ephemeral=True)


@bot.event
async def on_member_update(before, after):
    # Vérification via l'écran de règles natif de Discord
    if before.pending and not after.pending and db["member_role"]:
        try:
            await verify(after)
        except discord.Forbidden:
            pass


config = app_commands.Group(name="config", description="Configurer le bot",
                            default_permissions=discord.Permissions(administrator=True))


@config.command(name="unverified", description="Définir le rôle Unverified")
async def cfg_unv(inter: discord.Interaction, role: discord.Role):
    db["unverified_role"] = role.id
    save()
    await inter.response.send_message(f"✅ Unverified = {role.mention}", ephemeral=True)


@config.command(name="member", description="Définir le rôle Member (après règlement)")
async def cfg_mem(inter: discord.Interaction, role: discord.Role):
    db["member_role"] = role.id
    save()
    await inter.response.send_message(f"✅ Member = {role.mention}", ephemeral=True)


@config.command(name="logs", description="Définir le salon de logs")
async def cfg_logs(inter: discord.Interaction, salon: discord.TextChannel):
    db["log_channel"] = salon.id
    save()
    await inter.response.send_message(f"✅ Logs → {salon.mention}", ephemeral=True)


@config.command(name="hierarchie", description="Définir le salon de la hiérarchie")
async def cfg_hier(inter: discord.Interaction, salon: discord.TextChannel):
    db["hierarchy_channel"] = salon.id
    db["hierarchy_message"] = None
    save()
    await inter.response.defer(ephemeral=True)
    await update_hierarchy(inter.guild)
    await inter.followup.send(f"✅ Hiérarchie dans {salon.mention} (auto).")


bot.tree.add_command(config)


@bot.tree.command(name="regles_panel", description="Envoyer le panel d'acceptation du règlement")
@app_commands.default_permissions(administrator=True)
async def rules_panel(inter: discord.Interaction, texte: str, salon: discord.TextChannel = None):
    ch = salon or inter.channel
    e = discord.Embed(title="📜 Règlement", description=texte.replace("\\n", "\n"),
                      color=0xFEE75C)
    await ch.send(embed=e, view=RulesView())
    await inter.response.send_message("✅ Envoyé.", ephemeral=True)


@bot.tree.command(name="hierarchie_maj", description="Forcer la mise à jour de la hiérarchie")
@app_commands.default_permissions(manage_roles=True)
async def hier_now(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    await update_hierarchy(inter.guild)
    await inter.followup.send("✅ Mise à jour faite.")


# ====== TICKETS ======
def is_ticket(ch):
    return isinstance(ch, discord.TextChannel) and (ch.topic or "").startswith("ticket:")


def ticket_owner_id(ch):
    return int(ch.topic.split(":")[1])


def is_staff(member):
    rid = db["ticket_staff_role"]
    return member.guild_permissions.manage_channels or (
        rid and any(r.id == rid for r in member.roles))


async def close_ticket(ch, closer):
    # Transcript envoyé dans le salon de logs
    lines = []
    async for m in ch.history(limit=None, oldest_first=True):
        lines.append(f"[{m.created_at:%d/%m/%Y %H:%M}] {m.author}: {m.content}")
    cid = db["log_channel"]
    logch = ch.guild.get_channel(cid) if cid else None
    if logch:
        file = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")),
                            filename=f"{ch.name}.txt")
        await logch.send(f"🎫 Ticket `{ch.name}` fermé par {closer}.", file=file)
    await ch.send("🔒 Ticket fermé, suppression dans 5 secondes…")
    await asyncio.sleep(5)
    await ch.delete(reason=f"Ticket fermé par {closer}")


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Fermer le ticket", emoji="🔒",
                       style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close(self, inter, button):
        ch = inter.channel
        if not is_ticket(ch):
            return
        if inter.user.id != ticket_owner_id(ch) and not is_staff(inter.user):
            return await inter.response.send_message("❌ Pas la permission.", ephemeral=True)
        await inter.response.send_message("⏳ Fermeture…")
        await close_ticket(ch, inter.user)


class TicketOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Ouvrir un ticket", emoji="🎫",
                       style=discord.ButtonStyle.primary, custom_id="ticket_open")
    async def open(self, inter, button):
        g = inter.guild
        existing = discord.utils.find(
            lambda c: c.topic == f"ticket:{inter.user.id}", g.text_channels)
        if existing:
            return await inter.response.send_message(
                f"Tu as déjà un ticket : {existing.mention}", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        db["ticket_count"] += 1
        save()
        view_perms = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, attach_files=True,
            read_message_history=True)
        ow = {
            g.default_role: discord.PermissionOverwrite(view_channel=False),
            inter.user: view_perms,
            g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                              manage_channels=True),
        }
        staff = g.get_role(db["ticket_staff_role"]) if db["ticket_staff_role"] else None
        if staff:
            ow[staff] = view_perms
        cat = g.get_channel(db["ticket_category"]) if db["ticket_category"] else None
        if not isinstance(cat, discord.CategoryChannel):
            cat = None
        try:
            ch = await g.create_text_channel(
                f"ticket-{db['ticket_count']:04d}", category=cat, overwrites=ow,
                topic=f"ticket:{inter.user.id}")
        except discord.Forbidden:
            return await inter.followup.send("❌ Je ne peux pas créer le salon.")
        e = discord.Embed(
            title="🎫 Ticket ouvert",
            description=f"Salut {inter.user.mention}, explique ton problème.\n"
                        "Un membre du staff va te répondre.",
            color=0x5865F2)
        ping = staff.mention if staff else ""
        await ch.send(f"{inter.user.mention} {ping}", embed=e, view=TicketCloseView())
        await inter.followup.send(f"✅ Ticket créé : {ch.mention}")
        await log(g, f"🎫 Ticket {ch.mention} ouvert par {inter.user}.")


ticket = app_commands.Group(name="ticket", description="Système de tickets",
                            default_permissions=discord.Permissions(manage_channels=True))


@ticket.command(name="config", description="Définir la catégorie et le rôle staff")
async def ticket_cfg(inter: discord.Interaction, staff: discord.Role,
                     categorie: discord.CategoryChannel = None):
    db["ticket_staff_role"] = staff.id
    if categorie:
        db["ticket_category"] = categorie.id
    save()
    await inter.response.send_message(
        f"✅ Staff = {staff.mention}"
        + (f" • Catégorie = {categorie.name}" if categorie else ""), ephemeral=True)


@ticket.command(name="panel", description="Envoyer le panel d'ouverture de tickets")
async def ticket_panel(inter: discord.Interaction, salon: discord.TextChannel = None,
                       texte: str = "Besoin d'aide ? Clique sur le bouton pour ouvrir un ticket."):
    ch = salon or inter.channel
    e = discord.Embed(title="🎫 Support", description=texte, color=0x5865F2)
    await ch.send(embed=e, view=TicketOpenView())
    await inter.response.send_message("✅ Panel envoyé.", ephemeral=True)


@ticket.command(name="ajouter", description="Ajouter un membre au ticket")
async def ticket_add(inter: discord.Interaction, membre: discord.Member):
    if not is_ticket(inter.channel):
        return await inter.response.send_message("❌ Pas un ticket.", ephemeral=True)
    await inter.channel.set_permissions(membre, view_channel=True, send_messages=True)
    await inter.response.send_message(f"✅ {membre.mention} ajouté.")


@ticket.command(name="retirer", description="Retirer un membre du ticket")
async def ticket_remove(inter: discord.Interaction, membre: discord.Member):
    if not is_ticket(inter.channel):
        return await inter.response.send_message("❌ Pas un ticket.", ephemeral=True)
    await inter.channel.set_permissions(membre, overwrite=None)
    await inter.response.send_message(f"✅ {membre.mention} retiré.")


@ticket.command(name="fermer", description="Fermer le ticket actuel")
async def ticket_close(inter: discord.Interaction):
    if not is_ticket(inter.channel):
        return await inter.response.send_message("❌ Pas un ticket.", ephemeral=True)
    await inter.response.send_message("⏳ Fermeture…")
    await close_ticket(inter.channel, inter.user)


bot.tree.add_command(ticket)


# ====== ANTI-RAID / ANTI-SPAM ======
async def punish_join(member, reason):
    act = db["antiraid"]["action"]
    try:
        if act == "ban":
            await member.ban(reason=reason)
        elif act == "kick":
            await member.kick(reason=reason)
        else:
            await member.timeout(timedelta(hours=1), reason=reason)
        await log(member.guild, f"🛡️ **Anti-raid** : {member} ({act}) — {reason}")
    except discord.HTTPException:
        pass


@bot.event
async def on_member_join(member):
    global raid_until
    ar = db["antiraid"]
    if member.bot:
        return
    if ar["enabled"]:
        now = time.time()
        joins.append(now)
        while joins and now - joins[0] > ar["seconds"]:
            joins.popleft()
        if len(joins) >= ar["joins"] and now > raid_until:
            raid_until = now + 300
            await log(member.guild, "🚨 **RAID DÉTECTÉ** — mode raid actif 5 min.")
        age = (discord.utils.utcnow() - member.created_at).days
        if now < raid_until:
            return await punish_join(member, "Raid en cours")
        if age < ar["min_age_days"]:
            return await punish_join(member, f"Compte trop récent ({age} j)")
    ur = member.guild.get_role(db["unverified_role"]) if db["unverified_role"] else None
    if ur:
        try:
            await member.add_roles(ur, reason="Nouveau membre")
        except discord.Forbidden:
            pass


@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    ar = db["antiraid"]
    perms = message.author.guild_permissions
    if ar["enabled"] and not perms.manage_messages:
        if ar["block_invites"] and INVITE_RE.search(message.content):
            try:
                await message.delete()
                await message.channel.send(
                    f"{message.author.mention} les invitations sont interdites.",
                    delete_after=5)
            except discord.HTTPException:
                pass
            return
        q = msgs[message.author.id]
        now = time.time()
        q.append(now)
        while q and now - q[0] > ar["spam_seconds"]:
            q.popleft()
        if len(q) >= ar["spam_msgs"]:
            q.clear()
            try:
                await message.author.timeout(timedelta(minutes=10), reason="Spam")
                await message.channel.send(
                    f"🔇 {message.author.mention} mute 10 min (spam).", delete_after=8)
                await log(message.guild, f"🔇 {message.author} mute 10 min pour spam.")
            except discord.HTTPException:
                pass


antiraid = app_commands.Group(name="antiraid", description="Configurer l'anti-raid",
                              default_permissions=discord.Permissions(administrator=True))


@antiraid.command(name="statut", description="Voir la config anti-raid")
async def ar_status(inter: discord.Interaction):
    a = db["antiraid"]
    txt = (f"**Actif** : {a['enabled']}\n**Seuil raid** : {a['joins']} joins / {a['seconds']}s\n"
           f"**Âge min compte** : {a['min_age_days']} j\n**Action** : {a['action']}\n"
           f"**Anti-spam** : {a['spam_msgs']} msgs / {a['spam_seconds']}s\n"
           f"**Bloquer invitations** : {a['block_invites']}\n"
           f"**Mode raid** : {'OUI' if time.time() < raid_until else 'non'}")
    await inter.response.send_message(txt, ephemeral=True)


@antiraid.command(name="activer", description="Activer/désactiver l'anti-raid")
async def ar_toggle(inter: discord.Interaction, actif: bool):
    db["antiraid"]["enabled"] = actif
    save()
    await inter.response.send_message(f"Anti-raid : {'✅ ON' if actif else '❌ OFF'}",
                                      ephemeral=True)


@antiraid.command(name="config", description="Régler les seuils")
@app_commands.choices(action=[app_commands.Choice(name=n, value=n)
                              for n in ("kick", "ban", "timeout")])
async def ar_cfg(inter: discord.Interaction, joins: int = None, secondes: int = None,
                 age_min_jours: int = None, action: app_commands.Choice[str] = None,
                 spam_messages: int = None, spam_secondes: int = None,
                 bloquer_invitations: bool = None):
    a = db["antiraid"]
    if joins: a["joins"] = joins
    if secondes: a["seconds"] = secondes
    if age_min_jours is not None: a["min_age_days"] = age_min_jours
    if action: a["action"] = action.value
    if spam_messages: a["spam_msgs"] = spam_messages
    if spam_secondes: a["spam_seconds"] = spam_secondes
    if bloquer_invitations is not None: a["block_invites"] = bloquer_invitations
    save()
    await inter.response.send_message("✅ Config mise à jour.", ephemeral=True)


@antiraid.command(name="stop", description="Arrêter le mode raid")
async def ar_stop(inter: discord.Interaction):
    global raid_until
    raid_until = 0
    joins.clear()
    await inter.response.send_message("✅ Mode raid désactivé.", ephemeral=True)


bot.tree.add_command(antiraid)


# ====== MODÉRATION ======
@bot.tree.command(name="ban", description="Bannir un membre")
@app_commands.default_permissions(ban_members=True)
async def ban(inter: discord.Interaction, membre: discord.Member, raison: str = "Aucune"):
    if not can_act(inter, membre):
        return await deny(inter)
    await membre.ban(reason=f"{inter.user}: {raison}")
    await inter.response.send_message(f"🔨 {membre} banni. ({raison})")
    await log(inter.guild, f"🔨 {membre} banni par {inter.user} — {raison}")


@bot.tree.command(name="unban", description="Débannir via l'ID")
@app_commands.default_permissions(ban_members=True)
async def unban(inter: discord.Interaction, user_id: str):
    try:
        await inter.guild.unban(discord.Object(id=int(user_id)))
        await inter.response.send_message(f"✅ <@{user_id}> débanni.")
        await log(inter.guild, f"✅ {user_id} débanni par {inter.user}")
    except Exception:
        await inter.response.send_message("❌ ID invalide ou non banni.", ephemeral=True)


@bot.tree.command(name="kick", description="Expulser un membre")
@app_commands.default_permissions(kick_members=True)
async def kick(inter: discord.Interaction, membre: discord.Member, raison: str = "Aucune"):
    if not can_act(inter, membre):
        return await deny(inter)
    await membre.kick(reason=f"{inter.user}: {raison}")
    await inter.response.send_message(f"👢 {membre} expulsé. ({raison})")
    await log(inter.guild, f"👢 {membre} expulsé par {inter.user} — {raison}")


@bot.tree.command(name="mute", description="Timeout un membre (minutes)")
@app_commands.default_permissions(moderate_members=True)
async def mute(inter: discord.Interaction, membre: discord.Member,
               minutes: app_commands.Range[int, 1, 40320], raison: str = "Aucune"):
    if not can_act(inter, membre):
        return await deny(inter)
    await membre.timeout(timedelta(minutes=minutes), reason=f"{inter.user}: {raison}")
    await inter.response.send_message(f"🔇 {membre} mute {minutes} min. ({raison})")
    await log(inter.guild, f"🔇 {membre} mute {minutes} min par {inter.user} — {raison}")


@bot.tree.command(name="unmute", description="Retirer le timeout")
@app_commands.default_permissions(moderate_members=True)
async def unmute(inter: discord.Interaction, membre: discord.Member):
    await membre.timeout(None)
    await inter.response.send_message(f"🔊 {membre} unmute.")
    await log(inter.guild, f"🔊 {membre} unmute par {inter.user}")


@bot.tree.command(name="warn", description="Avertir un membre")
@app_commands.default_permissions(moderate_members=True)
async def warn(inter: discord.Interaction, membre: discord.Member, raison: str):
    if not can_act(inter, membre):
        return await deny(inter)
    db["warns"].setdefault(str(membre.id), []).append(
        {"raison": raison, "par": inter.user.id, "date": int(time.time())})
    save()
    n = len(db["warns"][str(membre.id)])
    await inter.response.send_message(f"⚠️ {membre.mention} averti ({n} warn). {raison}")
    await log(inter.guild, f"⚠️ {membre} warn par {inter.user} — {raison} (total {n})")


@bot.tree.command(name="warns", description="Voir les warns d'un membre")
@app_commands.default_permissions(moderate_members=True)
async def warns(inter: discord.Interaction, membre: discord.Member):
    w = db["warns"].get(str(membre.id), [])
    if not w:
        return await inter.response.send_message("Aucun warn.", ephemeral=True)
    txt = "\n".join(f"`{i+1}.` <t:{x['date']}:d> — {x['raison']}" for i, x in enumerate(w))
    await inter.response.send_message(f"**Warns de {membre}**\n{txt}", ephemeral=True)


@bot.tree.command(name="clearwarns", description="Effacer les warns d'un membre")
@app_commands.default_permissions(moderate_members=True)
async def clearwarns(inter: discord.Interaction, membre: discord.Member):
    db["warns"].pop(str(membre.id), None)
    save()
    await inter.response.send_message(f"🧹 Warns de {membre} effacés.", ephemeral=True)


@bot.tree.command(name="clear", description="Supprimer des messages")
@app_commands.default_permissions(manage_messages=True)
async def clear(inter: discord.Interaction, nombre: app_commands.Range[int, 1, 100]):
    await inter.response.defer(ephemeral=True)
    d = await inter.channel.purge(limit=nombre)
    await inter.followup.send(f"🧹 {len(d)} messages supprimés.")


@bot.tree.command(name="lock", description="Verrouiller un salon")
@app_commands.default_permissions(manage_channels=True)
async def lock(inter: discord.Interaction, salon: discord.TextChannel = None):
    ch = salon or inter.channel
    await ch.set_permissions(inter.guild.default_role, send_messages=False)
    await inter.response.send_message(f"🔒 {ch.mention} verrouillé.")


@bot.tree.command(name="unlock", description="Déverrouiller un salon")
@app_commands.default_permissions(manage_channels=True)
async def unlock(inter: discord.Interaction, salon: discord.TextChannel = None):
    ch = salon or inter.channel
    await ch.set_permissions(inter.guild.default_role, send_messages=None)
    await inter.response.send_message(f"🔓 {ch.mention} déverrouillé.")


@bot.tree.command(name="slowmode", description="Mode lent (secondes, 0 = off)")
@app_commands.default_permissions(manage_channels=True)
async def slowmode(inter: discord.Interaction, secondes: app_commands.Range[int, 0, 21600]):
    await inter.channel.edit(slowmode_delay=secondes)
    await inter.response.send_message(f"🐢 Slowmode : {secondes}s")


@bot.tree.command(name="lockdown", description="Verrouiller TOUS les salons texte")
@app_commands.default_permissions(administrator=True)
async def lockdown(inter: discord.Interaction, actif: bool):
    await inter.response.defer()
    for ch in inter.guild.text_channels:
        try:
            await ch.set_permissions(inter.guild.default_role,
                                     send_messages=False if actif else None)
        except discord.HTTPException:
            pass
    await inter.followup.send("🔒 Serveur verrouillé." if actif else "🔓 Serveur déverrouillé.")


@bot.tree.command(name="userinfo", description="Infos sur un membre")
async def userinfo(inter: discord.Interaction, membre: discord.Member = None):
    m = membre or inter.user
    e = discord.Embed(title=str(m), color=m.color)
    e.set_thumbnail(url=m.display_avatar.url)
    e.add_field(name="ID", value=m.id)
    e.add_field(name="Compte créé", value=discord.utils.format_dt(m.created_at, "R"))
    e.add_field(name="A rejoint", value=discord.utils.format_dt(m.joined_at, "R"))
    e.add_field(name="Warns", value=len(db["warns"].get(str(m.id), [])))
    await inter.response.send_message(embed=e)


@bot.tree.error
async def on_error(inter: discord.Interaction, error):
    msg = f"❌ Erreur : {error}"
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ Tu n'as pas la permission."
    if inter.response.is_done():
        await inter.followup.send(msg, ephemeral=True)
    else:
        await inter.response.send_message(msg, ephemeral=True)


@bot.event
async def on_ready():
    print(f"Connecté en tant que {bot.user} ✅")
    g = bot.get_guild(GUILD_ID)
    if g:
        await update_hierarchy(g)


bot.run(TOKEN)
