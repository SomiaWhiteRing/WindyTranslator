# core/api_clients/deepseek.py
import logging
from openai import OpenAI, APIConnectionError, AuthenticationError, RateLimitError, BadRequestError, OpenAIError

log = logging.getLogger(__name__)

class DeepSeekClient:
    """封装与 DeepSeek (或任何 OpenAI 兼容) API 的交互。"""

    def __init__(self, base_url, api_key):
        """
        初始化 OpenAI 兼容客户端。

        Args:
            base_url (str): API 的基础 URL (例如 "https://api.deepseek.com/v1" 或火山引擎的 URL)。
            api_key (str): API Key。
        """
        if not base_url:
            raise ValueError("API Base URL 不能为空。")
        if not api_key:
            raise ValueError("API Key 不能为空。")

        self.base_url = base_url
        self.api_key = api_key
        try:
            self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)
            log.info(f"OpenAI 兼容客户端初始化成功 (URL: {self.base_url})。")
        except Exception as e:
            log.exception(f"初始化 OpenAI 兼容客户端失败: {e}")
            raise ConnectionError(f"初始化 OpenAI 兼容客户端失败: {e}") from e

    def chat_completion(self, model_name, messages, temperature=0.7, max_tokens=None, **kwargs):
        """
        调用 Chat Completion API。

        Args:
            model_name (str): 要使用的模型名称。
            messages (list): 消息列表，格式如 [{"role": "user", "content": "..."}]。
            temperature (float, optional): 控制随机性的温度值。默认为 0.7。
            max_tokens (int, optional): 限制生成的最大 token 数。默认为 None (由模型决定)。
            **kwargs: 其他传递给 `client.chat.completions.create` 的参数。

        Returns:
            tuple: (success, result_content, error_message)
                   success (bool): API 调用是否成功并获得有效响应。
                   result_content (str): 如果成功，返回模型生成的消息内容；否则为 None。
                   error_message (str): 如果失败，返回错误信息；否则为 None。
        """
        if not model_name:
            return False, None, "模型名称不能为空。"
        if not messages:
            return False, None, "消息列表不能为空。"

        try:
            log.debug(f"向模型 '{model_name}' 发送 Chat Completion 请求...")
            # log.debug(f"Messages (概览): {[m.get('role', '?') for m in messages]}") # 避免记录完整内容

            response = self.client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs
            )

            if response.choices and response.choices[0].message and response.choices[0].message.content:
                content = response.choices[0].message.content
                log.debug("Chat Completion 成功返回响应内容。")
                return True, content, None
            else:
                # 检查是否有其他完成原因
                finish_reason = "未知"
                if response.choices and response.choices[0].finish_reason:
                    finish_reason = response.choices[0].finish_reason
                error_msg = f"Chat Completion 调用成功，但未返回有效内容。完成原因: {finish_reason}"
                log.warning(response)
                return False, None, error_msg # 标记为失败

        except AuthenticationError as e:
            error_msg = f"API 认证失败 (检查 API Key?): {e}"
            log.error(error_msg)
            return False, None, error_msg
        except RateLimitError as e:
            error_msg = f"API 请求频率超限: {e}"
            log.error(error_msg)
            return False, None, error_msg
        except APIConnectionError as e:
            error_msg = f"无法连接到 API 服务器 ({self.base_url}): {e}"
            log.error(error_msg)
            return False, None, error_msg
        except BadRequestError as e:
            # 通常是请求参数问题，例如 prompt 过长、模型不支持等
            error_msg = f"API 请求无效 (检查参数或 Prompt?): {e}"
            log.error(error_msg)
            return False, None, error_msg
        except OpenAIError as e: # 捕获其他 OpenAI SDK 定义的错误
            error_msg = f"OpenAI API 调用失败: {e}"
            log.exception(error_msg)
            return False, None, error_msg
        except Exception as e:
            error_msg = f"与 OpenAI 兼容 API 交互时发生意外错误: {e}"
            log.exception(error_msg)
            return False, None, error_msg

    def test_connection(self, model_name):
        """
        尝试与 API 进行简单的连接和认证测试。

        Args:
            model_name (str): 用于测试的模型名称。

        Returns:
            tuple: (success, message)
                   success (bool): 连接和认证测试是否成功。
                   message (str): 测试结果或错误信息。
        """
        log.info(f"测试与 OpenAI 兼容 API (模型: {model_name}, URL: {self.base_url}) 的连接...")
        # 使用一个模仿翻译的简单消息进行测试
        test_messages = [{"role": "user", "content": "请将以下文本的简体中文翻译结果包裹在<textarea>标签中并返回：「私は！　しまむらが知らないとこで笑っているとか！　嫌で、他の子と手を繫ぐのも！　私だけがよくて！　私と一緒にいてほしくて！　祭りだって、行きたかったし！　しまむらが楽しそうにしていると、笑っていると、その側に私がいて！　そういうのがよくて！　頭が痛いの、苦しいの！　しまむらのことばっかり考えて、どうかしそうに、なって……しまむらが電話してくれるのも待っているの！　たまには話してよ、私に話しかけてよ、私ばっかりじゃやだ、しまむらも、少しぐらい……少しは私のこと気にならない？　ちょっとも？　まったく？　なんでもないの？　友達だけ？　普通の友達なの？　普通じゃなくなりたいの、普通より一個でもいいから、普通じゃないのが、いい……ねぇ、しまむら、どうすればいいかな、ねぇ。しまむら聞いてる？　聞いて。私の声を聞いてなにか思う？　思ってくれる？　安心でもいいよなんでもいい、なにか思って。そういうのがほしい、そういうの求めちゃだめ？　しまむら！　しまむらなんだよぉ、私ね、しまむらがいいの。しまむら以外いらないし、いらない……しまむらだけでいいから。わがまま言ってないよ、一個だから、一個じゃん。みんななんてどうでもいいしいらないしあっちいっててほしいのになんでしまむらはそっちいくの、こっち来て、こっちに来て、側にいて、離れないで。嫌だ、しまむらの隣にいるのは私、私がいい、私がいたい、いさせて……だれあの子、私知らないよ。知らないしまむらになるのはやだ、しまむらのこと全部知っていたいし、知りたくないことあるのも嫌だし、でも知らないのはもっと嫌だし苦しいの。苦しい、痛い、痛い……しまむらぁ。しまむらと遊びに行こうって、言いたいのにお祭りだって行こうと思ってたんだよ、行きたいよ、でもしまむらあの子と行くの、遊んでいるの？　今どこにいるのしまむら、誰かといるの、しまむら、しまむらぁ……ねぇ聞いてる？　さっきから私ばっかりだよ喋ってるの。いつものしまむらはもっと喋ってくれるよね、ねぇなんで？　いつもみたいじゃない？　私おかしい？　おかしいよね、それは分かるんだよでも、知りたくて、しまむらのこと知りたくて、変になるの。しまむらと離れたくないのいつも一緒にいたいのどこでもいいの一緒ならどこでもいいから、しまむらと会ってないよ、会いたいよでも今会ったら泣きそうだし、泣いてるし、あの子とどうなんだろうなんなんだろうってそればかり気になっているしねぇ聞いてる？　私と一緒にいるよりあの子の方がいいの？　私だめ？　どこがだめ？　直すから言ってよ、直す絶対に直すだからお願い教えて、聞きたいの。しまむらはね、私、しまむらだから……しまむらだからっていうのがあるの、他の人がしまむらそっくりでも関係ないのいるはずないけど、ねぇそういうのじゃなくて、しまむらじゃないとダメなの。だから仲良くなりたいのに、なんか……こういうのじゃなくてもっと違う話したいけど、気になって……だってしまむら、笑顔だったよ？　私以外に笑うの、嫌だよ。嫌じゃない？　そうじゃない？　しまむらそういうのない？　しまむらって誰が好き？　好きな人いる？　好きになれる？　好きってなにか分かる？　時々ね、怖いの。しまむらはなんで隣にいてくれるんだろうって。しまむらと私ってそもそも友達だよね？　友達ぐらいにはなっているよね。友達と思ってくれてる？　しまむらは、そういうの……うぅう、ぇえ、しまむら、声聞かせて。声聞きたい、私のこと話して。しまむらが一番、私のこと分かって……分かってほしい。分かりたいし分かってほしい。一番になってほしい、なりたいの。なって、でも……ちょっと嫌なことがあるとくじけそうで……だってしまむらは、なんか、私を大事にしている感じがないから……大事、大事って変だけど、でも大事にしてほしい。大事がいいの！　他のと一緒にされるのやなの、本当に少しで、いいから……しまむら私のこと考えたことある？　夏休みに、ずっと会ってないけど、一回ぐらいは考えてくれた？　私ね、ずっと考えてた。しまむらのことしか考えてなかったよ。全部しまむら。だから、しまむらも！　私のこと、けっこう、考えて……しまむらと私は違うよ？　違うよね、分かってる、でも！　期待はするし、しちゃうし、こうしてうらぎ、られても……しまむらに電話したいって思うの。でも電話したってこうなって、どうにもならなくて、どうすればいいかな。ねぇ、しまむら、しまむら？　電話、繫がってるよね？　しまむらと繫がってるよね？　でも遠い、遠くて、会いたい。しまむらに直接会いたいの。笑ってほしいの、しまむらに頭を撫でてね、大丈夫って言ってほしいの。今どこにいるの？　どこ？　誰かといる？　あの子？　あの子だれ？　さっきから何回も聞いたよね、答えられないような相手なの？　どんな仲？　私より？　やだ、そんなのやだ、私よりなんて、やだって。やだ……違うって、違うって言って！　私、しまむらのこといっぱい考えてるよ！　足りない？　それじゃだめ？　もっと？　なにすればいい？　分かんないし、いつも考えても失敗するし、どういう私がいいのか教えて、教えてくれたら、私がんばるよ。絶対がんばるよ、だから、そんな子ほんとは、どっちでもいい。私が会いたいしまむらはもっと、別で、私が変わればいいだけって、わかってるけど……しまむら、ねぇ、しまむら。今なに考えてる？　私おかしい？　私へん？　しまむらの話をして。しまむらから私に声をかけて、しまむらから近づいて。いつも私ばっかり、ばっかり、ばっかり……一方通行じゃあ、こうなっちゃうよ！　こういうふうになっちゃうから、しまむらもこっち、に来て。しまむらは私嫌い？　違うよね？　やだよ、嫌いにならないで。嫌いいやだ。嫌いなのいやだ……好きに、好きになってほしい。だれか、好きになって。違うしまむらが好きに……嫌いなの？　お母さんみたいに私のこと嫌いなの？　声かけなくなるの？　知らない顔されるの？　私なんて言えばいいの？　なにすればいいの、飛べばいいの、跳ねればいいの、手を繫げばいいの、みんなやろうとしてでもやったら見てなくて……どうすればよかったの。どうすれば、誰も……しまむら、声、聞きたい……なにか言って、安心させて、でも他の人に笑うのやだ、私に笑って、笑って……頭痛い、お腹も、痛い……気になってたのになんで連絡、してくれなかったの。私に教えてよ、私知りたいの。しまむらのこと知りたいの、さっきからなんか、もう、気持ちぐるぐるで……同じこと言ってるけど、仕方ないよ、仕方ないじゃん、しまむらのことしか考えてないんだから……しまむらのことだけだから、ずっと、しまむらになっても……しまむらが、大事で、大事にしたくて、大事じゃないとやで、だから、私を見て。しまむら、見てないとやだ……他の子、なんてやだぁ……やなの。また行くの？　どこか行くの？　一緒に町行くの？　私と遊んだとこに、他の子と！　そんなの、やだよ。上書きしないでよ！　私、ずっと覚えてるのに、上書きされて……また行ったら、今度は違うの？　同じとこ見て違うもの見るの？　そんなのやだ、やだ、やだ。しまむらと一緒に、一緒のもの、分けて、分かって……おかしいよそんなの。違うよね私おかしいの、おかしいの分かるよ、でもおかしくなって……しまむらのこと、頭から離れなくて……今も……しまむら、しまむら、しま、むら……うぇう、う、うううう、しまむら、しまむら……っほ、げ、うぅ……しまむらの、しまむら？　しまむら、しまむら、しまむら……しまむらがいい、私は、いいから、だからしまむらも……ねぇお願い、しまむら……しまむらも、しまむら……」"}]
        success, content, error = self.chat_completion(model_name, test_messages)

        if success and content is not None: # 确保 content 不是空字符串等
            msg = "OpenAI 兼容 API 连接测试成功！"
            log.info(msg)
            return True, msg
        elif error:
            msg = f"OpenAI 兼容 API 连接测试失败: {error}"
            log.error(msg)
            return False, msg
        else: # success is False, but no specific error (e.g., empty response)
            msg = "OpenAI 兼容 API 连接测试失败: 未收到有效响应。"
            log.error(msg)
            return False, msg