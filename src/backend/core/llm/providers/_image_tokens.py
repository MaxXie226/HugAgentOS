"""Image-aware estimates shared by all model providers and SDK compression."""

from agentscope.message import DataBlock, Msg, ToolResultBlock

from core.llm.context_ir import IMAGE_TOKEN_RESERVE


class ImageTokenCountingMixin:
    """Estimate text normally and reserve image tokens independently of base64."""

    async def count_tokens(self, messages: list[Msg], tools: list[dict] | None) -> int:
        images = 0

        def without_images(blocks):
            nonlocal images
            kept = []
            for block in blocks:
                if isinstance(block, DataBlock) and block.source.media_type.startswith("image/"):
                    images += 1
                elif isinstance(block, ToolResultBlock) and isinstance(block.output, list):
                    kept.append(block.model_copy(update={"output": without_images(block.output)}))
                else:
                    kept.append(block)
            return kept

        # These copies are only for estimation; the provider receives the
        # original structured messages with every image byte intact.
        text_messages = [
            message.model_copy(update={"content": without_images(message.get_content_blocks())})
            for message in messages
        ]
        return (
            await super().count_tokens(messages=text_messages, tools=tools)
            + images * IMAGE_TOKEN_RESERVE
        )
