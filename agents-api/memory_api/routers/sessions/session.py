from typing import Tuple, Callable
import openai
from dataclasses import dataclass
from pydantic import UUID4
from memory_api.clients.cozo import client
from memory_api.clients.embed import embed
from memory_api.env import summarization_tokens_threshold
from memory_api.clients.temporal import run_summarization_task
from memory_api.models.entry.add_entries import add_entries_query
from memory_api.common.protocol.entries import Entry
from memory_api.clients.worker.types import ChatML
from memory_api.models.session.session_data import get_session_data
from memory_api.models.entry.proc_mem_context import proc_mem_context_query
from memory_api.autogen.openapi_model import InputChatMLMessage
from ...common.protocol.sessions import SessionData
from .protocol import Settings


tool_query_instruction = (
    "Transform this user request for fetching helpful tool descriptions: "
)
instruction_query_instruction = (
    "Embed this text chunk for finding useful historical chunks: "
)
doc_query_instruction = (
    "Encode this query and context for searching relevant passages: "
)


@dataclass
class BaseSession:
    session_id: UUID4
    developer_id: UUID4

    async def run(self, new_input, settings: Settings) -> Tuple[dict, Entry, Callable]:
        # TODO: implement locking at some point

        # Get session data
        session_data = get_session_data(self.developer_id, self.session_id)

        # Assemble context
        init_context, final_settings = await self.forward(
            session_data, new_input, settings
        )

        # Generate response
        response = await self.generate(init_context, final_settings)

        # Save response to session
        # if final_settings.get("remember"):
        #     await self.add_to_session(new_input, response)

        # TODO: this needs to be refactored somehow, no need to return 3rd value from this method
        message = response["choices"][0]["message"]

        total_tokens = response["usage"]["total_tokens"]
        completion_tokens = response["usage"]["completion_tokens"]
        new_entry = Entry(
            session_id=self.session_id,
            role=message["role"],
            name=None if session_data is None else session_data.agent_name,
            content=message["content"],
            token_count=completion_tokens,
        )

        # Return response and the backward pass as a background task (dont await here)
        backward_pass = await self.backward(
            new_input, total_tokens, new_entry, final_settings
        )

        return response, new_entry, backward_pass

    async def forward(
        self,
        session_data: SessionData | None,
        new_input: list[Entry],
        settings: Settings,
    ) -> Tuple[ChatML, Settings]:
        # role, name, content, token_count, created_at
        string_to_embed = "\n".join(
            [f"{msg.name or msg.role}: {msg.content}" for msg in new_input]
        )

        (
            tool_query_embedding,
            instruction_query_embedding,
            doc_query_embedding,
        ) = await embed(
            [
                instruction + string_to_embed
                for instruction in [
                    tool_query_instruction,
                    instruction_query_instruction,
                    doc_query_instruction,
                ]
            ],
            join_inputs=False,
        )

        entries: list[Entry] = []
        instructions = "IMPORTANT INSTRUCTIONS:\n\n"
        first_instruction_idx = -1
        first_instruction_created_at = 0
        for idx, row in client.run(
            proc_mem_context_query(
                session_id=self.session_id,
                tool_query_embedding=tool_query_embedding,
                instruction_query_embedding=instruction_query_embedding,
                doc_query_embedding=doc_query_embedding,
            )
        ).iterrows():
            if row["name"] != "instruction":
                entries.append(
                    Entry(
                        **{
                            "role": row["role"],
                            "name": row["name"],
                            "content": row["content"],
                            "session_id": self.session_id,
                            "created_at": row["created_at"],
                        }
                    )
                )
            else:
                if first_instruction_idx < 0:
                    first_instruction_idx = idx
                    first_instruction_created_at = row["created_at"]
                instructions += f"- {row['content']}\n"

        if first_instruction_idx >= 0:
            entries.insert(
                first_instruction_idx,
                Entry(
                    role="system",
                    name="instruction",
                    content=instructions,
                    session_id=self.session_id,
                    created_at=first_instruction_created_at,
                ),
            )

        messages = [
            {
                "role": e.role,
                "name": e.name,
                "content": e.content
                if not isinstance(e.content, list)
                else "\n".join(e.content),
            }
            for e in entries + new_input
            if e.content
        ]
        if session_data is not None:
            settings.model = session_data.model

        return messages, settings

    async def generate(self, init_context, settings: Settings) -> dict:
        # TODO: how to use response_format ?

        return openai.ChatCompletion.create(
            model=settings.model,
            messages=init_context,
            max_tokens=settings.max_tokens,
            stop=settings.stop,
            temperature=settings.temperature,
            frequency_penalty=settings.frequency_penalty,
            repetition_penalty=settings.repetition_penalty,
            best_of=1,
            top_p=settings.top_p,
            top_k=1,
            length_penalty=settings.length_penalty,
            # logit_bias=settings.logit_bias,
            presence_penalty=settings.presence_penalty,
            stream=settings.stream,
        )

    async def backward(
        self,
        new_input: list[InputChatMLMessage],
        total_tokens: int,
        new_entry: Entry,
        final_settings: Settings,
    ) -> None:
        if not final_settings.remember:
            return

        entries: list[Entry] = []
        for m in new_input:
            entries.append(
                Entry(
                    session_id=self.session_id,
                    role=m.role,
                    content=m.content,
                    name=m.name,
                )
            )

        entries.append(new_entry)
        client.run(add_entries_query(entries))

        if total_tokens >= summarization_tokens_threshold:
            return run_summarization_task


class PlainCompletionSession(BaseSession):
    pass


class RecursiveSummarizationSession(PlainCompletionSession):
    pass