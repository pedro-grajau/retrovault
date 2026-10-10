"""Resolve explicit titles deterministically and require a second consent command."""

from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid5

from app.modules.catalog.ports.repository import (
    CatalogReadUnavailable,
    PublishedCatalog,
)
from app.modules.commerce.ports.offers import CommerceReadUnavailable, OfferReader
from app.modules.concierge.domain.demand import (
    LIMITS,
    DemandsUnavailable,
    clean_description,
    normalize,
)
from app.modules.concierge.domain.session import IncomingMessage
from app.modules.concierge.ports.demands import DemandStore


class DemandService:
    def __init__(
        self, store: DemandStore, catalog: PublishedCatalog, offers: OfferReader
    ) -> None:
        self.store, self.catalog, self.offers = store, catalog, offers

    @staticmethod
    def owner(message: IncomingMessage) -> str:
        return sha256(
            f"{message.channel}:{message.external_user_id}".encode()
        ).hexdigest()

    @staticmethod
    def is_command(text: str) -> bool:
        return (
            text.split(maxsplit=1)[0].split("@")[0].lower()
            in {"/demanda", "/demandas", "/confirmar_demanda", "/cancelar_demanda"}
            if text.strip()
            else False
        )

    def resolve(self, title: str, platform: str) -> UUID | None:
        cursor = None
        matches: set[UUID] = set()
        while True:
            if 2 <= len(title) <= 100:
                hits, cursor = self.catalog.search_games(
                    query=title, limit=100, cursor=cursor
                )
                games = [hit.game for hit in hits]
            else:
                # Boundary titles cannot use Catalog's 2..100 search query.
                # Page its public projection while matching the full exact title.
                games, cursor = self.catalog.list_games(limit=100, cursor=cursor)
            matches.update(
                game.id
                for game in games
                if normalize(game.title) == normalize(title)
                and normalize(game.platform) == normalize(platform)
            )
            if cursor is None:
                break
        if len(matches) > 1:
            raise ValueError("ambiguous_title")
        return next(iter(matches), None)

    def unavailable(self, game_id: UUID | None, mode: str) -> bool:
        if game_id is None:
            return True
        return not any(
            o.mode == mode and o.available_units > 0
            for o in self.offers.list_offers([game_id]).get(game_id, [])
        )

    def handle(self, message: IncomingMessage, session_id: UUID) -> str:
        pieces = message.text.split(maxsplit=1)
        command = pieces[0] if pieces else ""
        argument = pieces[1] if len(pieces) > 1 else ""
        command = command.split("@")[0].lower()
        owner = self.owner(message)
        try:
            if command == "/demandas":
                records = self.store.list_owned(
                    owner_key=owner, channel=message.channel
                )
                lines: list[str] = []
                suffix = "Lista abreviada: há outros interesses. Você pode confirmar ou cancelar qualquer interesse usando o código já recebido no canal. Cancele os exibidos e consulte /demandas novamente para ver outros."
                for d in records:
                    line = f"{d.title} — {d.platform} ({'compra' if d.mode == 'purchase' else 'aluguel'}): {'ativo' if d.status == 'active' else 'aguardando consentimento'}; /cancelar_demanda {d.id}"
                    if len("\n".join([*lines, line, suffix])) > 3000:
                        break
                    lines.append(line)
                if len(lines) < len(records):
                    lines.append(suffix)
                return "\n".join(lines) or "Você não tem interesses ativos."
            if command in {"/cancelar_demanda", "/confirmar_demanda"}:
                try:
                    demand_id = UUID(argument.strip())
                except ValueError:
                    return "Informe o código do interesse mostrado na confirmação ou em /demandas."
                if command == "/cancelar_demanda":
                    owned = self.store.get_owned(
                        demand_id, owner_key=owner, channel=message.channel
                    )
                    if owned is None:
                        return "Interesse não encontrado para você neste canal."
                    if owned.status not in {"proposed", "active"}:
                        return "Este interesse já estava encerrado. Você não receberá novos avisos."
                    cancelled = self.store.cancel(
                        demand_id, owner_key=owner, channel=message.channel
                    )
                    return (
                        "Interesse encerrado. Você não receberá novos avisos."
                        if cancelled
                        else "Interesse não encontrado para você neste canal."
                    )
                owned = self.store.get_owned(
                    demand_id, owner_key=owner, channel=message.channel
                )
                if owned is None:
                    return "Interesse não encontrado para você neste canal."
                game_id = self.resolve(owned.title, owned.platform)
                if owned.status == "proposed" and not self.unavailable(
                    game_id, owned.mode
                ):
                    return "O título já tem disponibilidade. Consulte o catálogo; não registrei um novo interesse."
                demand = self.store.confirm(
                    demand_id,
                    owner_key=owner,
                    channel=message.channel,
                    update_id=message.update_id,
                )
                if demand is None:
                    return "Este interesse foi encerrado. Use /demanda para dar novo consentimento."
                if demand.status == "active" and demand.consented_at is not None:
                    try:
                        current_game = self.resolve(demand.title, demand.platform)
                        if current_game is not None and not self.unavailable(
                            current_game, demand.mode
                        ):
                            notice_id = uuid5(
                                NAMESPACE_URL,
                                f"retrovault:demand-consent-reconciliation:{demand.id}:{demand.consented_at.isoformat()}",
                            )
                            self.store.enqueue(
                                demand.id, event_id=notice_id, game_id=current_game
                            )
                    except CatalogReadUnavailable, CommerceReadUnavailable:
                        return f"Interesse registrado no Sandbox: {demand.title} — {demand.platform}. Não consegui reconsultar a disponibilidade agora. {LIMITS} Para cancelar: /cancelar_demanda {demand.id}"
                return f"Interesse registrado no Sandbox: {demand.title} — {demand.platform} ({'compra' if demand.mode == 'purchase' else 'aluguel'}). {LIMITS} Para cancelar: /cancelar_demanda {demand.id}"
            parts = [clean_description(part) for part in argument.split("|")]
            if len(parts) == 3:
                parts[2] = {"compra": "purchase", "aluguel": "rental"}.get(
                    parts[2].casefold(), parts[2].casefold()
                )
            if (
                len(parts) != 3
                or not parts[0]
                or not parts[1]
                or parts[2] not in {"purchase", "rental"}
            ):
                return "Para receber um aviso, use /demanda título | plataforma | compra ou aluguel. Depois confirme o consentimento."
            title, platform, mode = parts
            game_id = self.resolve(title, platform)
            if not self.unavailable(game_id, mode):
                return "O título já tem disponibilidade nessa modalidade. Consulte o catálogo."
            demand = self.store.propose(
                owner_key=owner,
                channel=message.channel,
                chat_id=message.external_chat_id,
                session_id=session_id,
                update_id=message.update_id,
                correlation_id=message.correlation_id,
                title=title,
                platform=platform,
                mode=mode,
                game_id=game_id,
                context={"version": "demand.v1", "limits": LIMITS},
            )
            return f"Quer receber um aviso no Sandbox sobre {demand.title} — {demand.platform} ({'compra' if demand.mode == 'purchase' else 'aluguel'})? {LIMITS} Confirme explicitamente com /confirmar_demanda {demand.id}. Para cancelar: /cancelar_demanda {demand.id}"
        except ValueError:
            return f"Há mais de um título correspondente a {clean_description(argument.split('|')[0])}. Informe a distinção do título e a plataforma; não associei um candidato."
        except CatalogReadUnavailable, CommerceReadUnavailable, DemandsUnavailable:
            return "Não consegui confirmar o catálogo ou a disponibilidade agora. Tente novamente; nenhum interesse foi registrado automaticamente."
