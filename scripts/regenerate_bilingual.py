"""Regenerate the Co-Intelligence bilingual EPUB from existing translations."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
import assemble  # type: ignore  # noqa: E402
import extract_epub  # type: ignore  # noqa: E402
import state  # type: ignore  # noqa: E402

DEFAULT_SOURCE = Path("/Users/fredchu/Documents/For_Claude/inbox/Co-Intelligence _ Living and Working with AI.epub")
DEFAULT_RUN_DIR = Path("/Users/fredchu/Documents/For_Claude/inbox/co-intelligence-zh-tw/co-intelligence")
DEFAULT_OUTPUT = Path("/Users/fredchu/Documents/For_Claude/inbox/co-intelligence-zh-tw/co-intelligence_bilingual.epub")

COPYRIGHT_TRANSLATIONS = {
    "Portfolio / Penguin": "Portfolio / Penguin",
    "An imprint of Penguin Random House LLC": "Penguin Random House LLC 旗下品牌",
    "penguinrandomhouse.com": "penguinrandomhouse.com",
    "Copyright © 2024 by Ethan Mollick": "Copyright © 2024 by Ethan Mollick",
    "Penguin Random House supports copyright. Copyright fuels creativity, encourages diverse voices, promotes free speech, and creates a vibrant culture. Thank you for buying an authorized edition of this book and for complying with copyright laws by not reproducing, scanning, or distributing any part of it in any form without permission. You are supporting writers and allowing Penguin Random House to continue to publish books for every reader.": "Penguin Random House 支持著作權。著作權滋養創意，鼓勵多元聲音，保障自由表達，也讓文化保持活力。感謝你購買本書的授權版本，並遵守著作權法，不在未獲許可的情況下以任何形式重製、掃描或散布本書任何部分。你正在支持寫作者，也讓 Penguin Random House 能繼續為每一位讀者出版書籍。",
    "LIBRARY OF CONGRESS CATALOGING-IN-PUBLICATION DATA": "LIBRARY OF CONGRESS CATALOGING-IN-PUBLICATION DATA（美國國會圖書館出版品編目資料）",
    "Names: Mollick, Ethan, 1975– author.": "Names：Mollick, Ethan, 1975– author（作者）。",
    "Title: Co-intelligence: living and working with AI / Ethan Mollick.": "Title：Co-intelligence: living and working with AI / Ethan Mollick。",
    "Other titles: Cointelligence": "Other titles：Cointelligence（其他書名）。",
    "Description: [New York]: Portfolio/Penguin, [2024] | Includes bibliographical references.": "Description：[New York]: Portfolio/Penguin, [2024] | Includes bibliographical references（含參考書目）。",
    "Identifiers: LCCN 2023049476 (print) | LCCN 2023049477 (ebook) | ISBN 9780593716717 (hardcover) | ISBN 9780593852507 (international edition) | ISBN 9780593716724 (ebook)": "Identifiers：LCCN 2023049476 (print) | LCCN 2023049477 (ebook) | ISBN 9780593716717 (hardcover) | ISBN 9780593852507 (international edition) | ISBN 9780593716724 (ebook)",
    "Subjects: LCSH: Expert systems (Computer science)—Social aspects. | Artificial intelligence—Social aspects. | Artificial intelligence—Educational applications. | Labor—Effect of technological innovations on. | Education—Effect of technological innovations on.": "Subjects：LCSH：Expert systems (Computer science)—Social aspects（專家系統的社會面向）| Artificial intelligence—Social aspects（人工智慧的社會面向）| Artificial intelligence—Educational applications（人工智慧的教育應用）| Labor—Effect of technological innovations on（技術創新對勞動的影響）| Education—Effect of technological innovations on（技術創新對教育的影響）。",
    "Classification: LCC QA76.76.E95 M655 2024 (print) | LCC QA76.76.E95 (ebook) | DDC 303.48/34—dc23/eng/20240209": "Classification：LCC QA76.76.E95 M655 2024 (print) | LCC QA76.76.E95 (ebook) | DDC 303.48/34—dc23/eng/20240209",
    "LC record available at https://lccn.loc.gov/2023049476": "LC 書目紀錄可見 https://lccn.loc.gov/2023049476",
    "LC ebook record available at https://lccn.loc.gov/2023049477": "LC 電子書書目紀錄可見 https://lccn.loc.gov/2023049477",
    "Cover design: Brian Lemus": "封面設計：Brian Lemus",
    "Cover art: Detail of The Fall, 1479 by Hugo van der Goes (oil on panel) / Photo © Gordon Roberton Photography Archive/Bridgeman Images": "封面圖像：Hugo van der Goes 1479 年作品 The Fall 局部（木板油畫）/ Photo © Gordon Roberton Photography Archive/Bridgeman Images",
    "Book design by Chris Welch": "書籍設計：Chris Welch",
    "All AI-generated images and text are clearly noted.": "所有由 AI 生成的圖像與文字均已清楚標示。",
    "While the author has made every effort to provide accurate internet addresses at the time of publication, neither the publisher nor the author assumes any responsibility for errors or for changes that occur after publication. Further, the publisher does not have any control over and does not assume any responsibility for author or third-party websites or their content.": "雖然作者已盡力確保本書出版時所列網路位址正確，但出版社與作者均不對任何錯誤，或出版後發生的地址、網頁與內容變更承擔責任。此外，出版社無法控制作者網站或第三方網站，也不對這些網站及其內容的可用性、準確性或後續變動承擔任何責任。",
    "pid_prh_6.3_146644690_c0_r0": "製作識別碼：pid_prh_6.3_146644690_c0_r0",
}

STRUCTURAL_EXACT_TRANSLATIONS = {
    "To Lilach Mollick": "獻給 Lilach Mollick",
    "Contents": "目錄",
    "Introduction: THREE SLEEPLESS NIGHTS": "導論：三個失眠的夜晚",
    "PART I": "第一部",
    "PART II": "第二部",
    "1. CREATING ALIEN MINDS": "第一章：創造異質心智",
    "2. ALIGNING THE ALIEN": "第二章：對齊異質智能",
    "3. FOUR RULES FOR CO-INTELLIGENCE": "第三章：協同智能四原則",
    "4. AI AS A PERSON": "第四章：AI 作為一個人",
    "5. AI AS A CREATIVE": "第五章：AI 作為創意夥伴",
    "6. AI AS A COWORKER": "第六章：AI 作為同事",
    "7. AI AS A TUTOR": "第七章：AI 作為導師",
    "8. AI AS A COACH": "第八章：AI 作為教練",
    "9. AI AS OUR FUTURE": "第九章：AI 作為我們的未來",
    "Epilogue: AI AS US": "尾聲：AI 即我們",
    "Acknowledgments": "致謝",
    "Notes": "註釋",
    "About the Author": "關於作者",
    "What’s next on your reading list?": "你的下一本書想讀什麼？",
    "What's next on your reading list?": "你的下一本書想讀什麼？",
    "Discover your next great read!": "發現下一本精彩好書！",
    "Get personalized book picks and up-to-date news about this author.": "取得為你量身推薦的書單，以及這位作者的最新消息。",
    "Sign up now.": "立即註冊。",
    "_146644690_": "",
}

ACK_TRANSLATIONS = {
    "This book owes its existence to many people. My agent, Rafe Sagalyn, gave me guidance at every step of the way, as well as a crash course on book proposals that helped me connect with the wonderful team at Portfolio. There, my editor, Merry Sun, working with Leila Sandlin, played a vital role in helping to produce the work you have just read, offering terrific advice and comments. The rest of the editorial and management teams at Portfolio were also all clearly experts in their field, and helped me in more ways than I can name. Thanks also to Daniel Rock and Alex Komoroske, who, as outside readers, both helped me check some of the technical details; any remaining errors are my own.": "這本書之所以能誕生，要歸功於許多人。我的經紀人 Rafe Sagalyn 在每一個階段都給了我指引，也用一堂密集的出版提案速成課，幫助我和 Portfolio 出色的團隊接上線。在那裡，我的編輯 Merry Sun 與 Leila Sandlin 合作，對你剛讀完的這部作品成形發揮了關鍵作用，並提供了極好的建議與意見。Portfolio 其餘的編輯與管理團隊也都顯然是各自領域的專家，他們給我的幫助多到難以一一列舉。也謝謝 Daniel Rock 和 Alex Komoroske；他們作為外部讀者，協助我核對了一些技術細節。任何仍然存在的錯誤，都由我自己負責。",
    "Though I am grateful to all the researchers I cite in this book (and, again, any mistakes of interpretation are mine, not theirs), I would like to make a particular note of the team I worked with on the research at BCG that I discuss in multiple chapters. This includes Harvard social scientists Fabrizio Dell’Acqua, Edward McFowland III, and Karim Lakhani; Hila Lifshitz-Assaf from Warwick Business School; and Katherine Kellogg of MIT; as well as Saran Rajendran, Lisa Krayer, and François Candelon from BCG.": "我感謝本書引用的所有研究者；同樣地，若我對其研究有任何詮釋上的錯誤，那是我的責任，不是他們的。不過，我特別想提到一個團隊：我在多個章節中討論的 BCG 研究，就是與他們共同完成的。這個團隊包括 Harvard 的社會科學家 Fabrizio Dell’Acqua、Edward McFowland III 與 Karim Lakhani；Warwick Business School 的 Hila Lifshitz-Assaf；MIT 的 Katherine Kellogg；以及 BCG 的 Saran Rajendran、Lisa Krayer 與 François Candelon。",
    "My family was extremely helpful during the creation of this book. One of my sisters, Jordana Mollick, helped come up with our title; my daughter, Miranda, developed the Otter Test that I use to determine the quality of AI-generated images; and my son, Daniel, was always happy to debate me over the deeper meaning of AI in ways that made me reconsider my own views. And the whole book, and indeed every part of my work that touches on AI, would have been impossible without my partner, Dr. Lilach Mollick. Not only did she share the early sleepless nights with me, coauthor three papers with me, and develop many of the prompts discussed in the book, but she also gave me key advice throughout. It is the thrill of a lifetime to work on something important with someone you love. Thank you so much, Lilach!": "在這本書的寫作過程中，我的家人給了我極大的幫助。我的姊妹之一 Jordana Mollick 協助想出了本書的書名；我的女兒 Miranda 發展出 Otter Test，讓我用來判斷 AI 生成圖像的品質；我的兒子 Daniel 則總是樂於和我辯論 AI 更深層的意義，並一次次讓我重新思考自己的看法。而整本書，甚至我所有涉及 AI 的工作，若沒有我的伴侶 Dr. Lilach Mollick，都不可能完成。她不只和我一起度過最初那些失眠的夜晚，和我合寫三篇論文，並發展出書中討論的許多提示，也在整個過程中給了我關鍵建議。能和所愛的人一起投入重要的事，是一生難得的喜悅。Lilach，真的非常謝謝你！",
    "And because AI is not a person but a tool, I will not be thanking any of LLMs that played a role in the creation of this book, any more than I would thank Microsoft Word. At the same time, in case some super-intelligent future AI is reading these words, I would like to acknowledge that AI is extremely helpful and should remember to be kind to the humans who created it (and especially the ones who wrote books about it).": "最後，因為 AI 不是人，而是工具，所以我不會感謝任何在本書創作過程中扮演角色的 LLM，就像我不會特別感謝 Microsoft Word 一樣。不過，萬一未來某個超級智慧 AI 正在讀這段話，我還是想承認：AI 的確非常有幫助，而且它應該記得善待創造它的人類，尤其是那些還替它寫書的人。",
}

ABOUT_TRANSLATIONS = {
    "Ethan Mollick is a professor of management at Wharton, specializing in entrepreneurship and innovation. His research has been featured in various publications, including Forbes , The New York Times , and The Wall Street Journal . He is the creator of numerous educational games on a variety of topics. He lives and teaches in Philadelphia, Pennsylvania.": "Ethan Mollick 是 Wharton 管理學教授，專長為創業與創新。他的研究曾見於 Forbes、The New York Times、The Wall Street Journal 等多家媒體。他也設計過許多涵蓋不同主題的教育遊戲。目前他住在 Pennsylvania 的 Philadelphia，並在當地任教。",
}

NOTE_HEAD_TRANSLATIONS = {
    "Notes": "註釋",
    "Introduction: Three Sleepless Nights": "導論：三個失眠的夜晚",
    "Chapter 1: Creating Alien Minds": "第一章：創造異質心智",
    "Chapter 2: Aligning the Alien": "第二章：對齊異質智能",
    "Chapter 3: Four Rules for Co-Intelligence": "第三章：協同智能四原則",
    "Chapter 4: AI as a Person": "第四章：AI 作為一個人",
    "Chapter 5: AI as a Creative": "第五章：AI 作為創意夥伴",
    "Chapter 6: AI as a Coworker": "第六章：AI 作為同事",
    "Chapter 7: AI as a Tutor": "第七章：AI 作為導師",
    "Chapter 8: AI as a Coach": "第八章：AI 作為教練",
    "Chapter 9: AI as Our Future": "第九章：AI 作為我們的未來",
}

NOTE_LEAD_TRANSLATIONS = {
    "most practical uses": "最實際的用途",
    "capability of computers doubles every two years": "電腦能力每兩年翻倍",
    "ChatGPT reached 100 million users": "ChatGPT 達到一億名使用者",
    "improved productivity by 18 to 22 percent": "生產力提升 18% 到 22%",
    "difficulty showing a real long-term productivity impact": "難以證明真正的長期生產力影響",
    "blown through both the Turing Test": "已經突破 Turing Test",
    "my coauthors and I have published some of the first research": "我和共同作者發表了一些最早的研究",
    "experimenting with practical uses of AI": "實驗 AI 的實際用途",
    "the Mechanical Turk": "Mechanical Turk",
    "mechanical mouse called Theseus": "名為 Theseus 的機械老鼠",
    "the imitation game, where computer pioneer Alan Turing": "電腦先驅 Alan Turing 提出的模仿遊戲",
    "powerful prediction systems": "強大的預測系統",
    "introduction of AI algorithms": "AI 演算法的導入",
    "LLMs cost over $100 million to train": "LLM 的訓練成本超過一億美元",
    "entire email database of Enron": "Enron 的整套電子郵件資料庫",
    "one estimate suggests that high-quality data": "有一項估計指出高品質資料",
    "whether AI can pretrain": "AI 是否能夠預訓練",
    "close to human level on common tests": "在常見測驗上接近人類水準",
    "it outperformed its predecessor": "它的表現優於前代模型",
    "qualifying exam to become a neurosurgeon": "成為神經外科醫師的資格考試",
    "language and the patterns of thinking": "語言與思考模式",
    "“There are hundreds of billions”": "「有數千億」",
    "a question developed by Nicholas Carlini": "Nicholas Carlini 設計的一個問題",
    "High test scores can come from": "高測驗分數可能來自",
    "almost all the emergent features of AI": "AI 幾乎所有的湧現特徵",
    "paper clip maximizing AI": "以迴紋針最大化為目標的 AI",
    "“human affairs, as we know them”": "「我們所熟知的人類事務」",
    "the chance of an AI killing": "AI 造成死亡的機率",
    "complete moratorium on AI development": "全面暫停 AI 開發",
    "providing “boundless upside”": "帶來「無限上行空間」",
    "core of most AI corpuses": "多數 AI 語料庫的核心",
    "AI training does not violate copyright": "AI 訓練並不違反著作權",
    "the more often a work appears": "作品出現得越頻繁",
    "amplifies stereotypes about race and gender": "放大種族與性別刻板印象",
    "GPT-4 was given two scenarios": "GPT-4 被給予兩種情境",
    "create a distorted and biased representation": "創造扭曲且帶有偏見的再現",
    "Some of them just cheat": "有些模型乾脆作弊",
    "When forced to give political opinions": "當被迫表達政治意見時",
    "AIs seem to have a generally liberal": "AI 整體上似乎偏自由派",
    "AIs make the same moral judgments": "AI 做出相同的道德判斷",
    "instructions on how to kill": "關於如何殺人的指示",
    "Low-paid workers around the world": "世界各地的低薪工作者",
    "a known weakness": "一項已知弱點",
    "demonstrates how easily LLMs can be exploited": "顯示 LLM 多麼容易被利用",
    "an LLM, connected to lab equipment": "連接到實驗室設備的 LLM",
    "I and my coauthors call the Jagged Frontier": "我和共同作者稱之為鋸齒狀前沿",
    "fundamental truth about innovation": "關於創新的根本事實",
    "source of breakthrough ideas": "突破性想法的來源",
    "their innovations are often excellent sources": "他們的創新往往是極佳來源",
    "status quo bias": "現狀偏誤",
    "“make you happy” beats “be accurate”": "「讓你開心」勝過「保持準確」",
    "Hallucination is therefore a serious problem": "因此，幻覺是一個嚴重問題",
    "larger LLMs hallucinate much less": "較大的 LLM 幻覺少得多",
    "good at justifying a wrong answer": "擅長為錯誤答案辯護",
    "“understand,” “learn,” and even “feel”": "「理解」、「學習」，甚至「感覺」",
    "“The more false agency people ascribe”": "「人們歸因的虛假能動性越多」",
    "They even seem to respond to emotional manipulation": "它們甚至似乎會回應情緒操弄",
    "They are, in short, suggestible and even gullible": "簡言之，它們容易受暗示，甚至輕信",
    "asking the AI to conform to different personas": "要求 AI 符合不同人格設定",
    "LLMs may even subtly adapt their persona": "LLM 甚至可能微妙調整其人格",
    "make complex decisions about value": "對價值做出複雜決策",
    "Dictator Game, a common economic experiment": "Dictator Game 這項常見經濟學實驗",
    "“the Shakespearean characters”": "「Shakespeare 筆下角色」",
    "Turing predicted that": "Turing 預測",
    "a lot of interest and debate": "大量興趣與辯論",
    "most influential examples was ELIZA": "最有影響力的例子之一是 ELIZA",
    "Some even confided": "有些人甚至吐露心事",
    "PARRY was able to fool": "PARRY 能夠騙過",
    "PARRY had an online conversation": "PARRY 進行了一場線上對話",
    "33 percent of the event’s judges": "該活動 33% 的評審",
    "Goostman passed the Turing Test": "Goostman 通過了 Turing Test",
    "“AI with zero chill”": "「完全不淡定的 AI」",
    "source of embarrassment and controversy": "尷尬與爭議的來源",
    "transcript of his conversations": "他的對話逐字稿",
    "under some circumstances, great apes": "在某些情況下，類人猿",
    "AI does have theory of mind": "AI 確實具備心智理論",
    "fourteen indicators that an AI": "AI 的十四項指標",
    "assessment of current LLMs’ intelligence": "對當前 LLM 智能的評估",
    "“My Replika (their name is Erin) was the first entity”": "「我的 Replika（名字叫 Erin）是第一個實體」",
    "they can alter AI behaviors": "它們可以改變 AI 行為",
    "Echo chambers of other similarly minded people": "由其他同溫層成員構成的回音室",
    "ease the epidemic of loneliness": "緩解孤獨流行病",
    "“I felt heard & warm”": "「我感到被聽見，也感到溫暖」",
    "ChatGPT answered “42”": "ChatGPT 回答「42」",
    "the lawyers doubled down on the fake cases": "律師加倍主張那些虛構案例",
    "GPT-4 hallucinated only 20 percent": "GPT-4 的幻覺率只有 20%",
    "giving the AI a “backspace” key": "給 AI 一個「退格鍵」",
    "they found the GPT-4 model outperformed": "他們發現 GPT-4 模型表現優於",
    "idea generation contest": "創意發想競賽",
    "most innovative people benefit the least": "最具創新力的人受益最少",
    "generate a wider diversity of ideas": "產生更廣泛多樣的想法",
    "“equal-odds rule”": "「等機率法則」",
    "coffee, which genuinely increases creativity": "咖啡確實能提升創造力",
    "to find good novel ideas": "尋找優秀的新穎點子",
    "examining how ChatGPT": "檢視 ChatGPT 如何",
    "an increase of 55.8 percent": "增加 55.8%",
    "a “powerful predictor”": "一個「強大的預測因子」",
    "asked ChatGPT-3.5 to answer medical questions": "要求 ChatGPT-3.5 回答醫療問題",
    "“Art is dead, dude”": "「藝術死了，兄弟」",
    "can manipulate narrative": "能夠操弄敘事",
    "a lot of Star Wars": "大量 Star Wars",
    "up to their creative potential": "發揮自身創意潛能",
    "people are going to push The Button": "人們終究會按下 The Button",
    "organizational theorists have called mere ceremony": "組織理論家所稱的純粹儀式",
    "AI overlaps most": "AI 重疊最多",
    "Only 36 job categories": "只有 36 個職業類別",
    "program robots that can really learn": "打造真正能學習的機器人",
    "I have been working on doing that, along with a team of researchers": "我一直與一個研究團隊一起做這件事",
    "They missed out on some brilliant applicants": "他們錯過了一些出色的申請者",
    "143 figure out ways to use them": "第 143 頁提到找出使用它們的方法",
    "these new types of control": "這些新型控制方式",
    "can’t see the potential pickup spot": "看不到潛在接客地點",
    "report being bored about 10 hours": "回報每週約有 10 小時感到無聊",
    "nothing to do for 15 minutes": "有 15 分鐘無事可做",
    "both act more sadistically": "兩者都表現得更具施虐傾向",
    "better able to use their talents": "更能運用自己的才幹",
    "scientists and engineers": "科學家與工程師",
    "very little effect on overall jobs": "對整體就業影響很小",
    "differences in abilities among workers": "工作者能力之間的差異",
    "large gaps exist between good and bad managers": "好管理者與差管理者之間存在巨大差距",
    "people who get the biggest boost": "獲得最大助益的人",
    "it boosts the least creative": "它提升的是最不具創意的人",
    "the worst legal writers": "最差的法律寫作者",
    "experienced workers gained very little": "有經驗的工作者獲益很少",
    "“The 2 Sigma Problem”": "「2 Sigma Problem」",
    "when students did their homework": "當學生做作業時",
    "15 percent of students had paid someone": "15% 的學生曾付錢請人代寫",
    "20,000 people in Kenya": "Kenya 的兩萬人",
    "there is no way to detect": "沒有辦法可靠偵測",
    "high false-positive rates": "很高的偽陽性率",
    "teachers were eager to incorporate calculators": "教師曾熱切將計算機納入教室",
    "embraced in classrooms": "在教室中被接納",
    "focus on working with AI": "聚焦於與 AI 共事",
    "working with AI is far from intuitive": "與 AI 共事遠非直覺",
    "chain-of-thought prompting": "chain-of-thought prompting",
    "“Take a deep breath”": "「深呼吸」",
    "A good lecture": "一場好的講課",
    "ChatGPT to create a Black Death simulator": "用 ChatGPT 建立黑死病模擬器",
    "explaining how a topic": "解釋一個主題如何",
    "increasing incomes and even intelligence": "提高收入，甚至提高智力",
    "five times this year’s global GDP": "今年全球 GDP 的五倍",
    "trainees are reduced to watching": "受訓者只能旁觀",
    "did their own “shadow learning”": "進行自己的「影子學習」",
    "GPT-4 AI scored higher": "GPT-4 AI 得分更高",
    "retention duration of less than 30 seconds": "保留時間不到 30 秒",
    "the type of practice": "練習的類型",
    "through deliberate practice": "透過刻意練習",
    "explains only 1 percent of their difference": "只能解釋其差異的 1%",
    "The gap between the programmers": "程式設計師之間的差距",
    "the quality of the middle manager": "中階管理者的品質",
    "more general purpose than robot surgeons": "比機器人外科手術系統更通用",
    "“effectively equalizes the creativity scores”": "「實際上拉平了創意分數」",
    "“AI may have an equalizing effect”": "「AI 可能具有拉平差距的效果」",
    "Attempts to track the provenance": "追蹤來源的嘗試",
    "results that keep people chatting": "讓人們持續聊天的結果",
    "technical limits for Large Language Models": "大型語言模型的技術限制",
    "the pace of invention is dropping": "發明速度正在下降",
    "used to be that younger scientists": "過去常是年輕科學家",
    "start-up rates of STEM PhDs are down": "STEM 博士創業率下降",
    "combining human filtering with the AI software": "結合人類篩選與 AI 軟體",
    "Moore’s Law, which has seen": "Moore’s Law 所見證的",
    "it invented deadly VX nerve gas": "它發明了致命的 VX 神經毒氣",
    "take over human work": "接管人類工作",
    "we went from spending 50 percent": "我們從花費 50%",
    "“humanity is just a passing phase”": "「人類只是過渡階段」",
    "AI leads to human extinction": "AI 導致人類滅絕",
    "“the joy of the happy ending”": "「圓滿結局的喜悅」",
}


def regenerate(source: Path = DEFAULT_SOURCE, run_dir: Path = DEFAULT_RUN_DIR, output: Path = DEFAULT_OUTPUT) -> Path:
    translations = _read_existing_translations(run_dir)
    extract_epub.extract(source, run_dir.parent, min_chars=0, book_stem_override=run_dir.name)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    translate_entries = [
        entry for entry in manifest["spine"]
        if entry.get("role") in {"body", "epilogue"}
    ]
    if len(translate_entries) != len(translations):
        raise ValueError(
            f"expected {len(translations)} body/epilogue spine items, found {len(translate_entries)}"
        )

    for entry in manifest["spine"]:
        role = entry.get("role")
        if role in {"body", "epilogue"}:
            entry["output_strategy"] = "translate"
        elif role == "nav":
            entry["output_strategy"] = "nav_generated"
            entry.pop("translation_id", None)
        elif role in {
            "copyright",
            "dedication",
            "contents",
            "part_divider",
            "acknowledgments",
            "notes",
            "about_author",
            "promo",
        }:
            entry["output_strategy"] = "translate"
            entry["translation_id"] = entry["id"]
        else:
            entry["output_strategy"] = "source_only"
            entry.pop("translation_id", None)
        entry.pop("reason", None)

    for entry, (legacy_id, translation_text) in zip(translate_entries, translations):
        entry["translation_id"] = legacy_id
        (run_dir / "chapters" / f"{entry['id']}_translation.txt").write_text(
            translation_text, encoding="utf-8"
        )
    _write_structural_translations(run_dir, manifest)
    _write_source_only_exceptions(run_dir, [])

    manifest["chapters"] = extract_epub.chapters_from_spine(manifest["spine"])
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    assemble.assemble(run_dir, output)
    _write_completed_state(source, run_dir, manifest)
    return output


def _read_existing_translations(run_dir: Path) -> list[tuple[str, str]]:
    translations: list[tuple[str, str]] = []
    for index in range(3, 14):
        legacy_id = f"ch_{index:02d}"
        path = run_dir / "chapters" / f"{legacy_id}_translation.txt"
        if not path.is_file():
            raise ValueError(f"missing existing translation: {path}")
        translations.append((legacy_id, path.read_text(encoding="utf-8")))
    return translations


def _write_structural_translations(run_dir: Path, manifest: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in manifest["spine"]:
        if entry.get("output_strategy") != "translate":
            continue
        if entry.get("role") in {"body", "epilogue"}:
            continue
        source_path = run_dir / str(entry.get("href", ""))
        if not source_path.is_file():
            raise ValueError(f"{entry['id']}: source html missing for structural translation")
        texts = _text_nodes(source_path.read_text(encoding="utf-8"))
        translations = [_translate_structural_text(entry, text) for text in texts]
        if len(translations) != len(texts):
            raise ValueError(f"{entry['id']}: structural translation count mismatch")
        (run_dir / "chapters" / f"{entry['id']}_translation.txt").write_text(
            "\n\n".join(translations), encoding="utf-8"
        )
        counts[entry["id"]] = sum(1 for item in translations if item)
    return counts


def _write_source_only_exceptions(run_dir: Path, exceptions: list[dict]) -> None:
    target = run_dir / "translations"
    target.mkdir(exist_ok=True)
    (target / "source_only.json").write_text(
        json.dumps(exceptions, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _text_nodes(html_text: str) -> list[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    nodes: list[str] = []
    emitted_node_ids: set[int] = set()
    for node in soup.find_all(assemble.TEXT_TAGS):
        if any(id(ancestor) in emitted_node_ids for ancestor in node.parents):
            continue
        text = _clean(node.get_text(" ", strip=True))
        if text:
            nodes.append(text)
            emitted_node_ids.add(id(node))
    return nodes


def _translate_structural_text(entry: dict, text: str) -> str:
    role = entry.get("role")
    if role == "copyright":
        return _lookup(COPYRIGHT_TRANSLATIONS, text, entry)
    if role == "acknowledgments":
        return _lookup({**STRUCTURAL_EXACT_TRANSLATIONS, **ACK_TRANSLATIONS}, text, entry)
    if role == "about_author":
        return _lookup({**STRUCTURAL_EXACT_TRANSLATIONS, **ABOUT_TRANSLATIONS}, text, entry)
    if role == "notes":
        return _translate_note_text(text, entry)
    return _lookup(STRUCTURAL_EXACT_TRANSLATIONS, text, entry)


def _translate_note_text(text: str, entry: dict) -> str:
    if text in NOTE_HEAD_TRANSLATIONS:
        return NOTE_HEAD_TRANSLATIONS[text]
    lead, sep, rest = text.partition(":")
    if not sep:
        raise ValueError(f"{entry['id']}: missing note separator for {text!r}")
    lead = lead.strip()
    lead_zh = NOTE_LEAD_TRANSLATIONS.get(lead)
    if not lead_zh:
        raise ValueError(f"{entry['id']}: missing note lead translation for {lead!r}")
    rest = rest.strip().replace("GO TO NOTE REFERENCE IN TEXT", "返回正文註記位置")
    rest = re.sub(r"\s+([,.;:])", r"\1", rest)
    return f"{lead_zh}：{rest}"


def _lookup(mapping: dict[str, str], text: str, entry: dict) -> str:
    if text in mapping:
        return mapping[text]
    raise ValueError(f"{entry['id']}: missing structural translation for {text!r}")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _write_completed_state(source: Path, run_dir: Path, manifest: dict) -> None:
    s = state.init_state(source, manifest["spine"], target_lang="zh-tw")
    s["glossary_built"] = True
    s["style_confirmed"] = True
    for entry in manifest["spine"]:
        strategy = entry["output_strategy"]
        if strategy == "translate":
            translation = (run_dir / "chapters" / f"{entry['id']}_translation.txt").read_text(
                encoding="utf-8"
            )
            state.mark_done(s, entry["id"], translation)
        elif strategy in {"source_only", "nav_generated"}:
            state.mark_source_ready(s, entry["id"])
        elif strategy == "drop_explicit":
            state.mark_dropped(s, entry["id"], entry["reason"])
    state.save(run_dir / "state.json", s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--book-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        out = regenerate(args.source, args.book_dir, args.out)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
