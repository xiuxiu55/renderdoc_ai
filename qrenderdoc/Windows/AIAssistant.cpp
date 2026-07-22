/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2026 Baldur Karlsson
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 ******************************************************************************/

#include "AIAssistant.h"
#include <algorithm>
#include <functional>
#include <QComboBox>
#include <QDateTime>
#include <QDesktopServices>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDir>
#include <QDirIterator>
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QFont>
#include <QFormLayout>
#include <QTextStream>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QKeyEvent>
#include <QLabel>
#include <QLineEdit>
#include <QMap>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QRegularExpression>
#include <QSettings>
#include <QTextCursor>
#include <QUrl>
#include "Code/QRDUtils.h"
#include "ui_AIAssistant.h"

namespace
{
const char *kSettingsGroup = "CodeBuddy";
const char *kDefaultBaseUrl = "http://127.0.0.1:8080";
}

AIAssistant::AIAssistant(ICaptureContext &ctx, QWidget *parent)
    : QFrame(parent), ui(new Ui::AIAssistant), m_Ctx(ctx)
{
  ui->setupUi(this);

  m_Net = new QNetworkAccessManager(this);

  applyTheme();
  loadSettings();

  QObject::connect(ui->modelCombo,
                   static_cast<void (QComboBox::*)(int)>(&QComboBox::activated), this,
                   &AIAssistant::onModelSelected);

  ui->inputEdit->installEventFilter(this);

  m_HealthTimer.setInterval(15000);
  QObject::connect(&m_HealthTimer, &QTimer::timeout, this, &AIAssistant::connectToCodeBuddy);

  appendMessage(
      lit("system"),
      lit("欢迎使用 <b>AI 助手 (CodeBuddy)</b>。<br/>"
          "先用 <code>codebuddy --serve --port 8080</code> 启动 CodeBuddy，然后点击"
          "<b>重新连接</b>。发送消息时可附带抓帧上下文（当前 EID / API）。"),
      true);

  setConnected(false, lit("未连接"));
  connectToCodeBuddy();
  m_HealthTimer.start();

  m_Ctx.AddCaptureViewer(this);
}

AIAssistant::~AIAssistant()
{
  m_HealthTimer.stop();
  cancelCurrentRun();

  if(m_HealthReply)
    m_HealthReply->abort();
  if(m_AcpReply)
    m_AcpReply->abort();

  m_Ctx.BuiltinWindowClosed(this);
  m_Ctx.RemoveCaptureViewer(this);
  delete ui;
}

void AIAssistant::OnCaptureLoaded()
{
  ui->executeLabel->setText(
      lit("已加载抓帧：%1").arg(QString(m_Ctx.GetCaptureFilename())));
}

void AIAssistant::OnCaptureClosed()
{
  ui->executeLabel->setText(lit("未加载抓帧"));
}

void AIAssistant::OnEventChanged(uint32_t eventId)
{
  if(!m_Ctx.IsCaptureLoaded())
    return;

  const ActionDescription *action = m_Ctx.CurAction();
  QString name = action ? QString(action->GetName(m_Ctx.GetStructuredFile())) : lit("-");
  ui->executeLabel->setText(tr("EID %1 - %2").arg(eventId).arg(name));
}

bool AIAssistant::eventFilter(QObject *watched, QEvent *event)
{
  if(watched == ui->inputEdit && event->type() == QEvent::KeyPress)
  {
    QKeyEvent *key = static_cast<QKeyEvent *>(event);
    if(key->key() == Qt::Key_Return || key->key() == Qt::Key_Enter)
    {
      if(key->modifiers() & Qt::ShiftModifier)
        return false;

      on_sendButton_clicked();
      return true;
    }
  }

  return QFrame::eventFilter(watched, event);
}

void AIAssistant::applyTheme()
{
  // Base UI font (kept modest so it inherits from the host on non-Windows too).
  QFont uiFont(lit("Segoe UI"));
  uiFont.setPointSize(10);
  uiFont.setStyleStrategy(QFont::PreferAntialias);
  setFont(uiFont);

  const QString dark = lit(
      "QFrame#AIAssistant, QFrame#footerFrame {"
      "  background-color: #1b1b1d;"
      "  color: #e6e6e6;"
      "  font-family: 'Segoe UI', 'Microsoft YaHei UI', sans-serif;"
      "}"
      "QFrame#footerFrame {"
      "  border-top: 1px solid #2c2c30;"
      "}"
      "QTextBrowser#chatView {"
      "  background-color: #1b1b1d;"
      "  color: #e6e6e6;"
      "  border: none;"
      "  padding: 12px;"
      "  selection-background-color: #2f5b86;"
      "}"
      // Scrollbars
      "QScrollBar:vertical {"
      "  background: transparent; width: 10px; margin: 2px;"
      "}"
      "QScrollBar::handle:vertical {"
      "  background: #3a3a40; min-height: 28px; border-radius: 5px;"
      "}"
      "QScrollBar::handle:vertical:hover { background: #4a4a52; }"
      "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
      "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }"
      // Input
      "QPlainTextEdit#inputEdit {"
      "  background-color: #26262a;"
      "  color: #f0f0f0;"
      "  border: 1px solid #3a3a40;"
      "  border-radius: 8px;"
      "  padding: 8px 10px;"
      "  font-size: 10pt;"
      "  selection-background-color: #2f5b86;"
      "}"
      "QPlainTextEdit#inputEdit:focus { border: 1px solid #3d8bfd; }"
      // Send button
      "QPushButton#sendButton {"
      "  background-color: #2f7fe0;"
      "  color: white;"
      "  font-weight: 600;"
      "  border: none;"
      "  border-radius: 8px;"
      "  padding: 7px 16px;"
      "  min-width: 68px;"
      "}"
      "QPushButton#sendButton:hover { background-color: #3d8bfd; }"
      "QPushButton#sendButton:pressed { background-color: #2569c0; }"
      "QPushButton#sendButton:disabled { background-color: #33343a; color: #7a7a80; }"
      // Cancel button
      "QPushButton#cancelButton {"
      "  background-color: #33343a;"
      "  color: #e0e0e0;"
      "  border: none;"
      "  border-radius: 8px;"
      "  padding: 7px 16px;"
      "  min-width: 68px;"
      "}"
      "QPushButton#cancelButton:hover { background-color: #43444c; }"
      "QPushButton#cancelButton:pressed { background-color: #2b2c31; }"
      "QPushButton#cancelButton:disabled { color: #7a7a80; }"
      // Labels / combos / tool buttons
      "QComboBox, QLabel, QToolButton {"
      "  color: #cfcfd4;"
      "  background: transparent;"
      "}"
      "QLabel#statusLabel { font-weight: 600; color: #e6e6e6; }"
      "QComboBox {"
      "  background-color: #26262a;"
      "  border: 1px solid #3a3a40;"
      "  border-radius: 6px;"
      "  padding: 3px 8px;"
      "  min-height: 22px;"
      "}"
      "QComboBox:hover { border: 1px solid #4a4a52; }"
      "QComboBox::drop-down { border: none; width: 18px; }"
      "QComboBox QAbstractItemView {"
      "  background-color: #26262a;"
      "  color: #e6e6e6;"
      "  border: 1px solid #3a3a40;"
      "  selection-background-color: #2f5b86;"
      "  outline: none;"
      "}"
      "QToolButton {"
      "  border: none;"
      "  border-radius: 6px;"
      "  padding: 4px 8px;"
      "}"
      "QToolButton:hover { background-color: #2f3036; }"
      "QToolButton:pressed { background-color: #26262a; }"
      "QLabel#executeLabel { color: #8a8a90; font-size: 9pt; }"
      "QLabel#modelLabel { color: #8a8a90; }");

  setStyleSheet(dark);
  ui->statusDot->setStyleSheet(
      lit("background-color: #c62828; border-radius: 5px; min-width: 10px; min-height: 10px;"));

  // Give the input a comfortable minimum height and a hint.
  ui->inputEdit->setMinimumHeight(44);
}

void AIAssistant::loadSettings()
{
  QSettings settings(lit("renderdoc"), lit("qrenderdoc"));
  settings.beginGroup(QString::fromUtf8(kSettingsGroup));
  m_BaseUrl = settings.value(lit("BaseUrl"), QString::fromUtf8(kDefaultBaseUrl)).toString();
  m_AuthToken = settings.value(lit("AuthToken")).toString();
  m_Model = settings.value(lit("Model")).toString();
  m_LastSkillDir = settings.value(lit("SkillDir")).toString();
  m_LastBatchDir = settings.value(lit("BatchDir")).toString();
  settings.endGroup();

  if(m_BaseUrl.endsWith(QLatin1Char('/')))
    m_BaseUrl.chop(1);
}

void AIAssistant::saveSettings()
{
  QSettings settings(lit("renderdoc"), lit("qrenderdoc"));
  settings.beginGroup(QString::fromUtf8(kSettingsGroup));
  settings.setValue(lit("BaseUrl"), m_BaseUrl);
  settings.setValue(lit("AuthToken"), m_AuthToken);
  settings.setValue(lit("Model"), m_Model);
  settings.setValue(lit("SkillDir"), m_LastSkillDir);
  settings.setValue(lit("BatchDir"), m_LastBatchDir);
  settings.endGroup();
}

void AIAssistant::setConnected(bool connected, const QString &detail)
{
  m_Connected = connected;
  ui->statusLabel->setText(connected ? lit("已连接") : lit("未连接"));
  ui->statusDot->setStyleSheet(connected ? lit("background-color: #2e7d32; border-radius: 5px;")
                                         : lit("background-color: #c62828; border-radius: 5px;"));

  // Hint how to bring CodeBuddy up when disconnected (shown on hover over the status).
  const QString hint =
      connected
          ? lit("已连接到 CodeBuddy。")
          : lit("未连接。请先启动 CodeBuddy 本地服务：\n"
                "codebuddy --serve --port 8080\n"
                "然后点击“重新连接”。");
  ui->statusLabel->setToolTip(hint);
  ui->statusDot->setToolTip(hint);

  if(!detail.isEmpty())
    ui->executeLabel->setText(detail);
}

void AIAssistant::setBusy(bool busy)
{
  m_Busy = busy;
  ui->sendButton->setEnabled(!busy && m_Connected);
  ui->cancelButton->setEnabled(busy);
  ui->inputEdit->setEnabled(!busy);
  ui->analyzeButton->setEnabled(!busy);
  ui->skillButton->setEnabled(!busy);
  ui->folderButton->setEnabled(!busy);
}

void AIAssistant::appendMessage(const QString &role, const QString &htmlOrText, bool asHtml)
{
  ChatMessage msg;
  msg.role = role;
  msg.content = htmlOrText;
  msg.html = asHtml;
  m_History.push_back(msg);
  refreshChatHtml();
}

void AIAssistant::appendAssistantDelta(const QString &text)
{
  if(text.isEmpty())
    return;

  m_AssistantBuffer += text;

  if(!m_AssistantOpen)
  {
    ChatMessage msg;
    msg.role = lit("assistant");
    msg.content = m_AssistantBuffer;
    msg.html = false;
    m_History.push_back(msg);
    m_AssistantOpen = true;
  }
  else
  {
    m_History.last().content = m_AssistantBuffer;
  }

  refreshChatHtml();
}

void AIAssistant::refreshChatHtml()
{
  QString html = lit(
      "<html><head><style>"
      "body { font-family: 'Segoe UI','Microsoft YaHei UI',sans-serif; font-size: 10pt;"
      "       color: #e6e6e6; line-height: 150%; }"
      ".msg { margin: 12px 0; }"
      ".role { font-size: 8pt; font-weight: 700; letter-spacing: 0.6px; margin-bottom: 4px; }"
      ".user .role { color: #6ea8fe; }"
      ".assistant .role { color: #8bc34a; }"
      ".bubble { padding: 2px 13px; border-radius: 10px; }"
      ".user .bubble { background:#243447; border:1px solid #2f4a68; }"
      ".assistant .bubble { background:#232326; border:1px solid #313135; }"
      ".system { color:#9a9aa0; font-size: 9pt; margin: 8px 2px; font-style: italic; }"
      "p { margin: 6px 0; }"
      "ul,ol { margin: 6px 0; padding-left: 22px; }"
      "li { margin: 3px 0; }"
      "blockquote { margin: 6px 0; padding: 2px 12px; border-left: 3px solid #3d8bfd;"
      "             color:#c7c7cf; background:#20232a; }"
      "hr { border: none; border-top: 1px solid #33343a; }"
      "code { background:#2b2b30; color:#e6c07b; padding:1px 5px; border-radius:4px;"
      "       font-family:'Cascadia Mono','Consolas',monospace; font-size: 9pt; }"
      "pre { background:#141416; border:1px solid #2c2c30; padding:10px; border-radius:8px;"
      "      margin:8px 0; }"
      "pre code { background:transparent; color:#dcdcdc; padding:0; }"
      "table { border-collapse:collapse; margin:8px 0; width:100%; }"
      "th { background:#2a2a30; color:#e6e6e6; text-align:left; font-weight:600; }"
      "td,th { border:1px solid #35353b; padding:5px 9px; font-size: 9pt; }"
      "h2,h3,h4 { color:#f0f0f0; margin:12px 0 5px 0; }"
      "h2 { font-size:13pt; } h3 { font-size:11.5pt; } h4 { font-size:10.5pt; }"
      "a { color:#6ea8fe; text-decoration:none; }"
      "</style></head><body>");

  for(const ChatMessage &msg : m_History)
  {
    QString body = msg.html ? msg.content : markdownishToHtml(msg.content);
    if(msg.role == lit("user"))
      html += lit("<div class='msg user'><div class='role'>你</div>"
                  "<div class='bubble'>%1</div></div>")
                  .arg(body);
    else if(msg.role == lit("assistant"))
      html += lit("<div class='msg assistant'><div class='role'>CodeBuddy</div>"
                  "<div class='bubble'>%1</div></div>")
                  .arg(body);
    else
      html += lit("<div class='system'>%1</div>").arg(body);
  }

  html += lit("</body></html>");
  ui->chatView->setHtml(html);

  QTextCursor c = ui->chatView->textCursor();
  c.movePosition(QTextCursor::End);
  ui->chatView->setTextCursor(c);
}

QString AIAssistant::escapeHtml(const QString &text) const
{
  QString out = text;
  out.replace(QLatin1Char('&'), lit("&amp;"));
  out.replace(QLatin1Char('<'), lit("&lt;"));
  out.replace(QLatin1Char('>'), lit("&gt;"));
  out.replace(QLatin1Char('"'), lit("&quot;"));
  return out;
}

// Inline markdown on already-HTML-escaped text: code spans, links, bold, italic.
static QString mdInline(const QString &escapedIn)
{
  QString s = escapedIn;

  // Protect inline code spans so bold/italic don't touch their contents.
  QVector<QString> codes;
  {
    QRegularExpression re(lit("`([^`]+)`"));
    QRegularExpressionMatchIterator it = re.globalMatch(s);
    QString rebuilt;
    int last = 0;
    while(it.hasNext())
    {
      QRegularExpressionMatch m = it.next();
      rebuilt += s.mid(last, m.capturedStart() - last);
      rebuilt += lit("\x03%1\x04").arg(codes.size());
      codes.push_back(m.captured(1));
      last = m.capturedEnd();
    }
    rebuilt += s.mid(last);
    s = rebuilt;
  }

  // Links [text](url)
  s.replace(QRegularExpression(lit("\\[([^\\]]+)\\]\\(([^)\\s]+)\\)")),
            lit("<a href=\"\\2\">\\1</a>"));
  // Bold
  s.replace(QRegularExpression(lit("\\*\\*([^*]+)\\*\\*")), lit("<b>\\1</b>"));
  s.replace(QRegularExpression(lit("__([^_]+)__")), lit("<b>\\1</b>"));
  // Italic (avoid touching ** already consumed)
  s.replace(QRegularExpression(lit("(?<![\\w*])\\*([^*\\n]+)\\*(?![\\w*])")), lit("<i>\\1</i>"));
  s.replace(QRegularExpression(lit("(?<![\\w_])_([^_\\n]+)_(?![\\w_])")), lit("<i>\\1</i>"));

  for(int i = 0; i < codes.size(); i++)
    s.replace(lit("\x03%1\x04").arg(i), lit("<code>%1</code>").arg(codes[i]));

  return s;
}

QString AIAssistant::markdownishToHtml(const QString &text) const
{
  QString src = text;
  src.replace(lit("\r\n"), lit("\n"));
  src.replace(QLatin1Char('\r'), QLatin1Char('\n'));

  // Pull fenced code blocks out first so their contents are not parsed as markdown.
  QVector<QString> codeBlocks;
  {
    QRegularExpression fence(lit("```[ \\t]*[a-zA-Z0-9_+\\-]*[ \\t]*\\n([\\s\\S]*?)```"));
    QRegularExpressionMatchIterator it = fence.globalMatch(src);
    QString rebuilt;
    int last = 0;
    while(it.hasNext())
    {
      QRegularExpressionMatch m = it.next();
      rebuilt += src.mid(last, m.capturedStart() - last);
      rebuilt += lit("\x01%1\x02").arg(codeBlocks.size());
      codeBlocks.push_back(m.captured(1));
      last = m.capturedEnd();
    }
    rebuilt += src.mid(last);
    src = rebuilt;
  }

  const QString escaped = escapeHtml(src);
  const QStringList lines = escaped.split(QLatin1Char('\n'));

  QString out;
  QStringList para;
  int listType = 0;    // 0 none, 1 ul, 2 ol
  bool inTable = false;
  bool inQuote = false;

  const QRegularExpression cbPlaceholder(lit("^\x01(\\d+)\x02$"));
  const QRegularExpression olRe(lit("^\\d+[.)]\\s+(.*)$"));
  const QRegularExpression sepRe(lit("^[\\s\\|:\\-]+$"));

  auto flushPara = [&]() {
    if(!para.isEmpty())
    {
      out += lit("<p>") + para.join(lit("<br/>")) + lit("</p>");
      para.clear();
    }
  };
  auto closeList = [&]() {
    if(listType == 1)
      out += lit("</ul>");
    else if(listType == 2)
      out += lit("</ol>");
    listType = 0;
  };
  auto closeTable = [&]() {
    if(inTable)
    {
      out += lit("</table>");
      inTable = false;
    }
  };
  auto closeQuote = [&]() {
    if(inQuote)
    {
      out += lit("</blockquote>");
      inQuote = false;
    }
  };
  auto closeBlocks = [&]() {
    flushPara();
    closeList();
    closeTable();
    closeQuote();
  };

  auto splitCells = [](const QString &row) {
    QString r = row.trimmed();
    if(r.startsWith(QLatin1Char('|')))
      r.remove(0, 1);
    if(r.endsWith(QLatin1Char('|')))
      r.chop(1);
    return r.split(QLatin1Char('|'));
  };

  for(int i = 0; i < lines.size(); i++)
  {
    const QString trimmed = lines[i].trimmed();

    // Fenced code block placeholder on its own line
    const QRegularExpressionMatch cb = cbPlaceholder.match(trimmed);
    if(cb.hasMatch())
    {
      closeBlocks();
      const int idx = cb.captured(1).toInt();
      const QString codeContent =
          (idx >= 0 && idx < codeBlocks.size()) ? codeBlocks[idx] : QString();
      out += lit("<pre><code>%1</code></pre>").arg(escapeHtml(codeContent));
      continue;
    }

    if(trimmed.isEmpty())
    {
      flushPara();
      closeQuote();
      closeList();
      closeTable();
      continue;
    }

    // Tables
    const bool isTableRow =
        trimmed.contains(QLatin1Char('|')) && trimmed.count(QLatin1Char('|')) >= 2;
    if(isTableRow)
    {
      flushPara();
      closeList();
      closeQuote();
      if(!inTable)
      {
        out += lit("<table>");
        inTable = true;
        const bool header = (i + 1 < lines.size()) &&
                            sepRe.match(lines[i + 1].trimmed()).hasMatch() &&
                            lines[i + 1].contains(QLatin1Char('-'));
        const QStringList cells = splitCells(trimmed);
        out += lit("<tr>");
        for(const QString &c : cells)
          out += (header ? lit("<th>%1</th>") : lit("<td>%1</td>")).arg(mdInline(c.trimmed()));
        out += lit("</tr>");
        if(header)
          i++;    // consume the separator row
      }
      else
      {
        if(sepRe.match(trimmed).hasMatch() && trimmed.contains(QLatin1Char('-')))
          continue;
        const QStringList cells = splitCells(trimmed);
        out += lit("<tr>");
        for(const QString &c : cells)
          out += lit("<td>%1</td>").arg(mdInline(c.trimmed()));
        out += lit("</tr>");
      }
      continue;
    }
    closeTable();

    // Headings
    if(trimmed.startsWith(lit("# ")) || trimmed.startsWith(lit("## ")) ||
       trimmed.startsWith(lit("### ")) || trimmed.startsWith(lit("#### ")))
    {
      closeBlocks();
      int level = 0;
      while(level < trimmed.size() && trimmed[level] == QLatin1Char('#'))
        level++;
      const int tag = qBound(2, level + 1, 4);
      out += lit("<h%1>%2</h%1>").arg(tag).arg(mdInline(trimmed.mid(level).trimmed()));
      continue;
    }

    // Horizontal rule
    if(trimmed == lit("---") || trimmed == lit("***") || trimmed == lit("___"))
    {
      closeBlocks();
      out += lit("<hr/>");
      continue;
    }

    // Blockquote ('>' was escaped to '&gt;')
    if(trimmed.startsWith(lit("&gt; ")) || trimmed == lit("&gt;"))
    {
      flushPara();
      closeList();
      closeTable();
      if(!inQuote)
      {
        out += lit("<blockquote>");
        inQuote = true;
      }
      const QString content = trimmed.startsWith(lit("&gt; ")) ? trimmed.mid(5) : QString();
      out += mdInline(content) + lit("<br/>");
      continue;
    }
    closeQuote();

    // Unordered list
    if(trimmed.startsWith(lit("- ")) || trimmed.startsWith(lit("* ")) ||
       trimmed.startsWith(lit("+ ")))
    {
      flushPara();
      closeTable();
      if(listType != 1)
      {
        closeList();
        out += lit("<ul>");
        listType = 1;
      }
      out += lit("<li>%1</li>").arg(mdInline(trimmed.mid(2).trimmed()));
      continue;
    }

    // Ordered list
    const QRegularExpressionMatch ol = olRe.match(trimmed);
    if(ol.hasMatch())
    {
      flushPara();
      closeTable();
      if(listType != 2)
      {
        closeList();
        out += lit("<ol>");
        listType = 2;
      }
      out += lit("<li>%1</li>").arg(mdInline(ol.captured(1).trimmed()));
      continue;
    }

    // Normal paragraph text
    closeList();
    para << mdInline(trimmed);
  }

  closeBlocks();

  // Restore any stray inline code-block placeholders left inside paragraphs.
  for(int i = 0; i < codeBlocks.size(); i++)
    out.replace(lit("\x01%1\x02").arg(i),
                lit("<pre><code>%1</code></pre>").arg(escapeHtml(codeBlocks[i])));

  return out;
}

QString AIAssistant::buildCaptureContext() const
{
  if(!m_Ctx.IsCaptureLoaded())
    return tr("No capture is currently loaded in RenderDoc.");

  QString ctx = tr("RenderDoc capture context:\n");
  ctx += tr("- File: %1\n").arg(QString(m_Ctx.GetCaptureFilename()));
  ctx += tr("- API: %1\n").arg(ToQStr(m_Ctx.APIProps().pipelineType));
  ctx += tr("- Selected EID: %1\n").arg(m_Ctx.CurSelectedEvent());
  ctx += tr("- Current EID: %1\n").arg(m_Ctx.CurEvent());

  const ActionDescription *action = m_Ctx.CurAction();
  if(action)
  {
    ctx += tr("- Action: %1\n").arg(QString(action->GetName(m_Ctx.GetStructuredFile())));
    ctx += tr("- flags: 0x%1\n").arg(QString::number((uint32_t)action->flags, 16));
    if(action->numIndices)
      ctx += tr("- numIndices: %1\n").arg(action->numIndices);
    if(action->numInstances)
      ctx += tr("- numInstances: %1\n").arg(action->numInstances);
  }

  ctx += tr("- Textures: %1\n").arg(m_Ctx.GetTextures().count());
  ctx += tr("- Buffers: %1\n").arg(m_Ctx.GetBuffers().count());
  ctx += tr("- Resources: %1\n").arg(m_Ctx.GetResources().count());

  return ctx;
}

// Chinese keywords are written as UTF-8 byte escapes so the source stays pure ASCII
// and compiles regardless of the compiler's code page (MSVC defaults to GBK/936 here).
static bool matchesAny(const QString &q, const char *const *kw, int count)
{
  for(int i = 0; i < count; i++)
    if(q.contains(QString::fromUtf8(kw[i]), Qt::CaseInsensitive))
      return true;
  return false;
}

bool AIAssistant::questionNeedsMemory(const QString &q) const
{
  static const char *kw[] = {
      "memory", "vram", "texture", "buffer", "size", "alloc", "footprint",
      "\xE5\x86\x85\xE5\xAD\x98",    // memory
      "\xE6\x98\xBE\xE5\xAD\x98",    // vram
      "\xE7\xBA\xB9\xE7\x90\x86",    // texture
      "\xE8\xB4\xB4\xE5\x9B\xBE",    // texture map
      "\xE7\xBC\x93\xE5\x86\xB2",    // buffer
      "\xE5\x8D\xA0\xE7\x94\xA8",    // usage
      "\xE5\xA4\xA7\xE5\xB0\x8F",    // size
      "\xE8\xB5\x84\xE6\xBA\x90",    // resource
  };
  return matchesAny(q, kw, (int)(sizeof(kw) / sizeof(kw[0])));
}

bool AIAssistant::questionNeedsTiming(const QString &q) const
{
  static const char *kw[] = {
      "slow", "perf", "performance", "fps", "frame rate", "bottleneck", "timing", "duration",
      "gpu time", "expensive", "cost",
      "\xE6\x85\xA2",                // slow
      "\xE8\x80\x97\xE6\x97\xB6",    // time cost
      "\xE6\x80\xA7\xE8\x83\xBD",    // performance
      "\xE5\xB8\xA7\xE7\x8E\x87",    // frame rate
      "\xE5\x8D\xA1\xE9\xA1\xBF",    // stutter
      "\xE7\x93\xB6\xE9\xA2\x88",    // bottleneck
      "\xE6\x97\xB6\xE9\x97\xB4",    // time
      "\xE5\xBC\x80\xE9\x94\x80",    // overhead
  };
  return matchesAny(q, kw, (int)(sizeof(kw) / sizeof(kw[0])));
}

bool AIAssistant::questionNeedsActions(const QString &q) const
{
  static const char *kw[] = {
      "drawcall", "draw call", "draw", "dispatch", "event", "pass", "count", "how many",
      "\xE7\xBB\x98\xE5\x88\xB6",    // draw
      "\xE4\xBA\x8B\xE4\xBB\xB6",    // event
      "\xE9\x80\x9A\xE9\x81\x93",    // pass
      "\xE5\xA4\x9A\xE5\xB0\x91",    // how many
      "\xE6\x95\xB0\xE9\x87\x8F",    // count
      "\xE8\xB0\x83\xE7\x94\xA8",    // call
  };
  return matchesAny(q, kw, (int)(sizeof(kw) / sizeof(kw[0])));
}

QString AIAssistant::assembleQuestionPrompt(const QString &userText, const QString &dataContext) const
{
  if(dataContext.trimmed().isEmpty())
    return userText;

  return tr("You are a graphics debugging assistant with access to a RenderDoc capture. The data "
            "below was gathered on-demand from RenderDoc's own API based on the user's question. "
            "Use it to give a specific, data-grounded answer.\n\n"
            "=== RenderDoc data ===\n%1\n=== End data ===\n\nUser question:\n%2")
      .arg(dataContext, userText);
}

void AIAssistant::sendQuestion(const QString &userText)
{
  const bool ctxNone = ui->contextCombo->currentIndex() == 1;
  const bool haveCapture = m_Ctx.IsCaptureLoaded();

  if(ctxNone || !haveCapture)
  {
    startRun(assembleQuestionPrompt(userText, QString()));
    return;
  }

  QString dataContext = buildCaptureContext();
  if(questionNeedsMemory(userText))
    dataContext += lit("\n") + buildMemoryReport();
  if(questionNeedsActions(userText))
    dataContext += lit("\n") + buildActionSummary();

  if(questionNeedsTiming(userText))
  {
    ui->executeLabel->setText(lit("正在从 RenderDoc 采集 GPU 耗时……"));
    setBusy(true);
    fetchTimingReport([this, userText, dataContext](QString timing) {
      QString ctx = dataContext;
      if(!timing.isEmpty())
        ctx += lit("\n") + timing;
      startRun(assembleQuestionPrompt(userText, ctx));
    });
    return;
  }

  startRun(assembleQuestionPrompt(userText, dataContext));
}

QString AIAssistant::formatNetworkError(QNetworkReply *reply) const
{
  if(!reply)
    return tr("Unknown network error");

  const QByteArray body = reply->readAll();
  QJsonDocument doc = QJsonDocument::fromJson(body);
  if(doc.isObject())
  {
    QJsonObject err = doc.object().value(lit("error")).toObject();
    QString msg = err.value(lit("message")).toString();
    QString code = err.value(lit("code")).toString();
    if(!msg.isEmpty())
    {
      if(!code.isEmpty())
        return lit("%1 (%2)").arg(msg, code);
      return msg;
    }
  }

  if(!body.isEmpty())
    return QString::fromUtf8(body);

  return reply->errorString();
}

QNetworkRequest AIAssistant::makeRequest(const QString &path) const
{
  QUrl url(m_BaseUrl + path);
  QNetworkRequest req(url);
  req.setHeader(QNetworkRequest::ContentTypeHeader, lit("application/json"));
  req.setRawHeader("X-CodeBuddy-Request", "1");
  req.setRawHeader("Accept", "application/json, text/event-stream");
  if(!m_AuthToken.isEmpty())
    req.setRawHeader("Authorization", QByteArray("Bearer ") + m_AuthToken.toUtf8());
  return req;
}

// ACP requests carry the per-connection id + session token instead of the user token.
QNetworkRequest AIAssistant::makeAcpRequest(const QString &path) const
{
  QUrl url(m_BaseUrl + path);
  QNetworkRequest req(url);
  req.setHeader(QNetworkRequest::ContentTypeHeader, lit("application/json"));
  req.setRawHeader("X-CodeBuddy-Request", "1");
  req.setRawHeader("Accept", "application/json, text/event-stream");
  if(!m_ConnectionId.isEmpty())
    req.setRawHeader("acp-connection-id", m_ConnectionId.toUtf8());
  if(!m_SessionToken.isEmpty())
    req.setRawHeader("Authorization", QByteArray("Bearer ") + m_SessionToken.toUtf8());
  return req;
}

QByteArray AIAssistant::rpcBody(const QString &method, const QJsonObject &params)
{
  QJsonObject o;
  o[lit("jsonrpc")] = lit("2.0");
  o[lit("id")] = ++m_RpcId;
  o[lit("method")] = method;
  o[lit("params")] = params;
  return QJsonDocument(o).toJson(QJsonDocument::Compact);
}

void AIAssistant::connectToCodeBuddy()
{
  if(m_HealthReply)
    return;

  QNetworkRequest req = makeRequest(lit("/api/v1/health"));
  m_HealthReply = m_Net->get(req);
  QObject::connect(m_HealthReply, &QNetworkReply::finished, this, &AIAssistant::healthCheckFinished);
}

void AIAssistant::healthCheckFinished()
{
  QNetworkReply *reply = m_HealthReply;
  m_HealthReply = NULL;
  if(!reply)
    return;

  reply->deleteLater();

  if(reply->error() != QNetworkReply::NoError)
  {
    setConnected(false, lit("无法连接到 CodeBuddy：%1（%2）")
                            .arg(m_BaseUrl)
                            .arg(reply->errorString()));
    ui->sendButton->setEnabled(false);
    return;
  }

  QJsonDocument doc = QJsonDocument::fromJson(reply->readAll());
  QJsonObject root = doc.object();
  QJsonObject payload = root.value(lit("data")).toObject();
  QString status = payload.value(lit("status")).toString();
  if(status.isEmpty())
    status = lit("ok");

  setConnected(true, lit("CodeBuddy %1 - %2").arg(status).arg(m_BaseUrl));
  if(!m_Busy)
    ui->sendButton->setEnabled(true);

  if(!m_AcpReady && !m_AcpConnecting)
    startAcpHandshake();
}

// Establish an ACP session: /acp/connect -> initialize -> session/new. The model
// list (availableModels) and current model come back from session/new, which is the
// only place CodeBuddy exposes the same models selectable in its own UI.
void AIAssistant::startAcpHandshake()
{
  if(m_AcpConnecting || m_AcpReply)
    return;

  m_AcpConnecting = true;
  m_AcpReady = false;
  m_AcpSessionId.clear();
  m_ConnectionId.clear();
  m_SessionToken.clear();

  QNetworkRequest req = makeAcpRequest(lit("/api/v1/acp/connect"));
  m_AcpReply = m_Net->post(req, QByteArray("{}"));
  QObject::connect(m_AcpReply, &QNetworkReply::finished, this, &AIAssistant::acpConnectFinished);
}

void AIAssistant::acpConnectFinished()
{
  QNetworkReply *reply = m_AcpReply;
  m_AcpReply = NULL;
  if(!reply)
    return;

  reply->deleteLater();

  if(reply->error() != QNetworkReply::NoError)
  {
    m_AcpConnecting = false;
    ui->executeLabel->setText(tr("ACP connect failed: %1").arg(formatNetworkError(reply)));
    return;
  }

  QJsonObject o = QJsonDocument::fromJson(reply->readAll()).object();
  if(o.value(lit("data")).isObject())
    o = o.value(lit("data")).toObject();

  m_ConnectionId = o.value(lit("connectionId")).toString();
  m_SessionToken = o.value(lit("sessionToken")).toString();

  if(m_ConnectionId.isEmpty())
  {
    m_AcpConnecting = false;
    ui->executeLabel->setText(tr("ACP connect returned no connection id."));
    return;
  }

  QJsonObject params;
  params[lit("protocolVersion")] = 1;
  params[lit("clientCapabilities")] = QJsonObject();

  QNetworkRequest req = makeAcpRequest(lit("/api/v1/acp"));
  m_AcpReply = m_Net->post(req, rpcBody(lit("initialize"), params));
  QObject::connect(m_AcpReply, &QNetworkReply::finished, this, &AIAssistant::acpInitFinished);
}

void AIAssistant::acpInitFinished()
{
  QNetworkReply *reply = m_AcpReply;
  m_AcpReply = NULL;
  if(!reply)
    return;

  reply->deleteLater();

  if(reply->error() != QNetworkReply::NoError)
  {
    m_AcpConnecting = false;
    ui->executeLabel->setText(tr("ACP initialize failed: %1").arg(formatNetworkError(reply)));
    return;
  }

  QString cwd;
  if(m_Ctx.IsCaptureLoaded())
    cwd = QFileInfo(QString(m_Ctx.GetCaptureFilename())).absolutePath();
  if(cwd.isEmpty())
    cwd = QDir::homePath();

  QJsonObject params;
  params[lit("cwd")] = cwd;
  params[lit("mcpServers")] = QJsonArray();

  QNetworkRequest req = makeAcpRequest(lit("/api/v1/acp"));
  m_AcpReply = m_Net->post(req, rpcBody(lit("session/new"), params));
  QObject::connect(m_AcpReply, &QNetworkReply::finished, this, &AIAssistant::acpNewSessionFinished);
}

// The POST response for an ACP request is itself an SSE stream; scan it for the
// JSON-RPC result object that carries the sessionId / models.
static QJsonObject parseAcpResult(const QString &body)
{
  const QStringList lines = body.split(QLatin1Char('\n'));
  for(const QString &raw : lines)
  {
    QString line = raw;
    if(line.endsWith(QLatin1Char('\r')))
      line.chop(1);
    if(!line.startsWith(lit("data:")))
      continue;

    QString payload = line.mid(5).trimmed();
    if(payload.isEmpty())
      continue;

    QJsonDocument d = QJsonDocument::fromJson(payload.toUtf8());
    if(!d.isObject())
      continue;

    QJsonObject o = d.object();
    if(o.value(lit("result")).isObject())
    {
      QJsonObject r = o.value(lit("result")).toObject();
      if(r.contains(lit("sessionId")) || r.contains(lit("models")))
        return r;
    }
  }
  return QJsonObject();
}

void AIAssistant::acpNewSessionFinished()
{
  QNetworkReply *reply = m_AcpReply;
  m_AcpReply = NULL;
  m_AcpConnecting = false;
  if(!reply)
    return;

  reply->deleteLater();

  if(reply->error() != QNetworkReply::NoError)
  {
    ui->executeLabel->setText(tr("ACP session failed: %1").arg(formatNetworkError(reply)));
    return;
  }

  const QJsonObject result = parseAcpResult(QString::fromUtf8(reply->readAll()));

  m_AcpSessionId = result.value(lit("sessionId")).toString();

  const QJsonObject models = result.value(lit("models")).toObject();
  const QJsonArray avail = models.value(lit("availableModels")).toArray();
  const QString currentId = models.value(lit("currentModelId")).toString();

  QVector<QPair<QString, QString>> list;
  list.reserve(avail.size());
  for(const QJsonValue &v : avail)
  {
    const QJsonObject m = v.toObject();
    QString id = m.value(lit("modelId")).toString();
    if(id.isEmpty())
      id = m.value(lit("id")).toString();
    QString name = m.value(lit("name")).toString();
    if(id.isEmpty())
      continue;
    if(name.isEmpty())
      name = id;
    list.push_back(qMakePair(id, name));
  }

  m_AcpReady = !m_AcpSessionId.isEmpty();
  populateModels(list, currentId);

  if(m_AcpReady && !m_Busy)
    ui->executeLabel->setText(lit("CodeBuddy 就绪（%1 个可选模型）- %2")
                                  .arg(list.count())
                                  .arg(m_BaseUrl));

  // If the user previously chose a model that differs from the session default, apply it.
  if(m_AcpReady && !m_Model.isEmpty() && m_Model != currentId && m_Model != lit("codebuddy"))
    applyModelSelection(m_Model);
}

void AIAssistant::populateModels(const QVector<QPair<QString, QString>> &models,
                                 const QString &current)
{
  m_PopulatingModels = true;

  ui->modelCombo->clear();
  if(models.isEmpty())
  {
    ui->modelCombo->addItem(lit("codebuddy"), lit("codebuddy"));
  }
  else
  {
    for(const QPair<QString, QString> &m : models)
      ui->modelCombo->addItem(m.second, m.first);    // display name, userData = modelId
  }

  // Prefer the user's saved model, then the server's current model, then the first entry.
  const QString target = !m_Model.isEmpty() ? m_Model : current;
  int idx = target.isEmpty() ? -1 : ui->modelCombo->findData(target);
  if(idx < 0)
    idx = 0;

  ui->modelCombo->setCurrentIndex(idx);
  m_Model = ui->modelCombo->currentData().toString();
  if(m_Model.isEmpty())
    m_Model = ui->modelCombo->currentText();

  m_PopulatingModels = false;
}

void AIAssistant::onModelSelected(int index)
{
  if(m_PopulatingModels || index < 0)
    return;

  m_Model = ui->modelCombo->itemData(index).toString();
  if(m_Model.isEmpty())
    m_Model = ui->modelCombo->itemText(index);
  saveSettings();
  applyModelSelection(m_Model);
}

void AIAssistant::applyModelSelection(const QString &modelId)
{
  if(modelId.isEmpty() || modelId == lit("codebuddy"))
    return;
  if(m_AcpSessionId.isEmpty())
    return;

  QJsonObject params;
  params[lit("sessionId")] = m_AcpSessionId;
  params[lit("modelId")] = modelId;

  QNetworkRequest req = makeAcpRequest(lit("/api/v1/acp"));
  QNetworkReply *reply = m_Net->post(req, rpcBody(lit("session/set_model"), params));
  QObject::connect(reply, &QNetworkReply::finished, reply, &QNetworkReply::deleteLater);

  ui->executeLabel->setText(lit("已切换模型：%1").arg(ui->modelCombo->currentText()));
}

void AIAssistant::on_sendButton_clicked()
{
  QString text = ui->inputEdit->toPlainText().trimmed();
  if(text.isEmpty() || m_Busy)
    return;

  if(!m_Connected)
  {
    appendMessage(lit("system"), lit("尚未连接到 CodeBuddy，请点击“重新连接”。"), true);
    connectToCodeBuddy();
    return;
  }

  appendMessage(lit("user"), text, false);
  ui->inputEdit->clear();

  sendQuestion(text);
}

void AIAssistant::on_cancelButton_clicked()
{
  cancelCurrentRun();
}

void AIAssistant::on_reconnectButton_clicked()
{
  m_AcpReady = false;
  m_AcpSessionId.clear();
  connectToCodeBuddy();
}

void AIAssistant::on_settingsButton_clicked()
{
  QDialog dlg(this);
  dlg.setWindowTitle(lit("CodeBuddy 设置"));

  // The panel-wide dark stylesheet dims child text; give the dialog its own
  // high-contrast theme so all labels and inputs are clearly readable.
  dlg.setStyleSheet(lit(
      "QDialog { background-color: #232327; }"
      "QLabel { color: #f0f0f0; background: transparent; font-size: 10pt; }"
      "QLineEdit {"
      "  background-color: #2b2b30; color: #f5f5f5; border: 1px solid #4a4a52;"
      "  border-radius: 6px; padding: 5px 8px; selection-background-color: #2f5b86; }"
      "QLineEdit:focus { border: 1px solid #3d8bfd; }"
      "QPushButton {"
      "  background-color: #33343a; color: #f0f0f0; border: 1px solid #4a4a52;"
      "  border-radius: 6px; padding: 5px 14px; }"
      "QPushButton:hover { background-color: #43444c; }"
      "QPushButton:default { background-color: #2f7fe0; border: none; color: white; }"
      "QPushButton:default:hover { background-color: #3d8bfd; }"));

  QFormLayout *form = new QFormLayout(&dlg);

  QLineEdit *urlEdit = new QLineEdit(m_BaseUrl, &dlg);
  QLineEdit *tokenEdit = new QLineEdit(m_AuthToken, &dlg);
  tokenEdit->setEchoMode(QLineEdit::Password);
  tokenEdit->setPlaceholderText(lit("可选的 Bearer 令牌 / 密码"));

  form->addRow(lit("服务地址"), urlEdit);
  form->addRow(lit("鉴权令牌"), tokenEdit);
  form->addRow(new QLabel(lit("启动服务：codebuddy --serve --port 8080"), &dlg));

  QDialogButtonBox *buttons =
      new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dlg);
  form->addRow(buttons);
  QObject::connect(buttons, &QDialogButtonBox::accepted, &dlg, &QDialog::accept);
  QObject::connect(buttons, &QDialogButtonBox::rejected, &dlg, &QDialog::reject);

  if(dlg.exec() != QDialog::Accepted)
    return;

  m_BaseUrl = urlEdit->text().trimmed();
  if(m_BaseUrl.endsWith(QLatin1Char('/')))
    m_BaseUrl.chop(1);
  if(m_BaseUrl.isEmpty())
    m_BaseUrl = QString::fromUtf8(kDefaultBaseUrl);
  m_AuthToken = tokenEdit->text();
  saveSettings();
  m_AcpReady = false;
  m_AcpSessionId.clear();
  connectToCodeBuddy();
}

static QString formatBytes(uint64_t bytes)
{
  const double mb = double(bytes) / (1024.0 * 1024.0);
  if(mb >= 1.0)
    return QString::number(mb, 'f', 2) + lit(" MB");
  return QString::number(double(bytes) / 1024.0, 'f', 1) + lit(" KB");
}

namespace
{
// One capture file's extracted metrics, produced on the batch worker thread.
struct BatchFileResult
{
  QString fileName;
  QString api;
  int texCount = 0;
  int bufCount = 0;
  uint64_t texTotal = 0;
  uint64_t bufTotal = 0;
  bool hasTiming = false;
  double gpuTotalSecs = 0.0;
  int eventCount = 0;
  QString detail;    // concise per-file markdown section
  QString error;     // non-empty if the file could not be analysed
};

// Returns the resource a draw/dispatch primarily writes to: the first non-null
// colour render target, falling back to the depth target. Used to attribute a
// timed event's GPU cost to a concrete resource.
static ResourceId primaryOutputResource(const ActionDescription *a)
{
  if(!a)
    return ResourceId();
  for(int i = 0; i < 8; i++)
    if(a->outputs[i] != ResourceId())
      return a->outputs[i];
  return a->depthOut;
}

// Opens, replays and extracts metrics for a single .rdc file. MUST run on a
// dedicated worker thread (all IReplayController calls must stay on the thread
// that created the controller). Does not touch ICaptureContext or any UI.
static BatchFileResult analyzeCaptureFile(const QString &path)
{
  BatchFileResult res;
  res.fileName = QFileInfo(path).fileName();

  ICaptureFile *file = RENDERDOC_OpenCaptureFile();
  if(!file)
  {
    res.error = lit("无法创建 CaptureFile 对象");
    return res;
  }

  const QByteArray pathUtf8 = path.toUtf8();
  ResultDetails openRes = file->OpenFile(pathUtf8.data(), "rdc", NULL);
  if(!openRes.OK())
  {
    res.error = lit("打开失败：%1").arg(QString(openRes.Message()));
    file->Shutdown();
    return res;
  }

  if(file->LocalReplaySupport() == ReplaySupport::Unsupported)
  {
    res.error = lit("该抓帧无法在本机回放");
    file->Shutdown();
    return res;
  }

  IReplayController *ctrl = NULL;
  ResultDetails replayRes;
  rdctie(replayRes, ctrl) = file->OpenCapture(ReplayOptions(), NULL);
  file->Shutdown();

  if(!replayRes.OK() || ctrl == NULL)
  {
    res.error = lit("回放初始化失败：%1").arg(QString(replayRes.Message()));
    return res;
  }

  res.api = ToQStr(ctrl->GetAPIProperties().pipelineType);

  QMap<ResourceId, QString> names;
  for(const ResourceDescription &r : ctrl->GetResources())
    names.insert(r.resourceId, QString(r.name));

  const rdcarray<TextureDescription> &textures = ctrl->GetTextures();
  const rdcarray<BufferDescription> &buffers = ctrl->GetBuffers();

  res.texCount = textures.count();
  res.bufCount = buffers.count();
  for(const TextureDescription &t : textures)
    res.texTotal += t.byteSize;
  for(const BufferDescription &b : buffers)
    res.bufTotal += b.length;

  QString detail;
  detail += lit("### 文件：%1\n").arg(res.fileName);
  detail += lit("- 图形 API：%1\n").arg(res.api);
  detail += lit("- 纹理 %1 个，合计 %2；缓冲区 %3 个，合计 %4；资源内存合计 %5\n")
                .arg(res.texCount)
                .arg(formatBytes(res.texTotal))
                .arg(res.bufCount)
                .arg(formatBytes(res.bufTotal))
                .arg(formatBytes(res.texTotal + res.bufTotal));

  // Top textures
  QVector<int> texIdx;
  texIdx.reserve(textures.count());
  for(int i = 0; i < textures.count(); i++)
    texIdx.push_back(i);
  std::sort(texIdx.begin(), texIdx.end(),
            [&textures](int a, int b) { return textures[a].byteSize > textures[b].byteSize; });

  const int kTopTex = 5;
  detail += lit("\n占用最大的纹理：\n| 大小 | 尺寸 | 资源 |\n|---|---|---|\n");
  for(int i = 0; i < texIdx.count() && i < kTopTex; i++)
  {
    const TextureDescription &t = textures[texIdx[i]];
    QString dims = lit("%1x%2").arg(t.width).arg(t.height);
    if(t.depth > 1)
      dims += lit("x%1").arg(t.depth);
    if(t.arraysize > 1)
      dims += lit("[%1]").arg(t.arraysize);
    if(t.mips > 1)
      dims += lit(" %1 级 mip").arg(t.mips);
    detail += lit("| %1 | %2 | %3 |\n")
                  .arg(formatBytes(t.byteSize))
                  .arg(dims)
                  .arg(names.value(t.resourceId, lit("-")));
  }

  // Top buffers
  QVector<int> bufIdx;
  bufIdx.reserve(buffers.count());
  for(int i = 0; i < buffers.count(); i++)
    bufIdx.push_back(i);
  std::sort(bufIdx.begin(), bufIdx.end(),
            [&buffers](int a, int b) { return buffers[a].length > buffers[b].length; });

  const int kTopBuf = 5;
  detail += lit("\n占用最大的缓冲区：\n| 大小 | 资源 |\n|---|---|\n");
  for(int i = 0; i < bufIdx.count() && i < kTopBuf; i++)
  {
    const BufferDescription &b = buffers[bufIdx[i]];
    detail += lit("| %1 | %2 |\n")
                  .arg(formatBytes(b.length))
                  .arg(names.value(b.resourceId, lit("-")));
  }

  // GPU timing
  const rdcarray<GPUCounter> avail = ctrl->EnumerateCounters();
  if(avail.contains(GPUCounter::EventGPUDuration))
  {
    const CounterDescription desc = ctrl->DescribeCounter(GPUCounter::EventGPUDuration);
    rdcarray<GPUCounter> want;
    want.push_back(GPUCounter::EventGPUDuration);
    const rdcarray<CounterResult> results = ctrl->FetchCounters(want);
    const bool wide = desc.resultByteWidth == 8;

    const SDFile &sdfile = ctrl->GetStructuredFile();
    QMap<uint32_t, QString> actionNames;
    QMap<uint32_t, ResourceId> actionOutput;
    std::function<void(const rdcarray<ActionDescription> &)> walk =
        [&](const rdcarray<ActionDescription> &list) {
          for(const ActionDescription &a : list)
          {
            actionNames.insert(a.eventId, QString(a.GetName(sdfile)));
            actionOutput.insert(a.eventId, primaryOutputResource(&a));
            walk(a.children);
          }
        };
    walk(ctrl->GetRootActions());

    double total = 0.0;
    QVector<QPair<double, uint32_t>> events;
    events.reserve((int)results.size());
    for(const CounterResult &r : results)
    {
      double secs = wide ? r.value.d : double(r.value.f);
      total += secs;
      events.push_back(qMakePair(secs, r.eventId));
    }
    std::sort(events.begin(), events.end(),
              [](const QPair<double, uint32_t> &a, const QPair<double, uint32_t> &b) {
                return a.first > b.first;
              });

    res.hasTiming = true;
    res.gpuTotalSecs = total;
    res.eventCount = events.count();

    detail += lit("\n- GPU 总耗时：%1 ms").arg(total * 1000.0, 0, 'f', 3);
    if(total > 0.0)
      detail += lit("，估算 GPU 瓶颈帧率 %1 FPS").arg(1.0 / total, 0, 'f', 1);
    detail += lit("\n");

    const int kTopEvt = 10;
    detail += lit("\n耗时最高的事件：\n| EID | GPU 毫秒 | 目标资源 | 事件 |\n|---|---|---|---|\n");
    for(int i = 0; i < events.count() && i < kTopEvt; i++)
    {
      const ResourceId target = actionOutput.value(events[i].second, ResourceId());
      detail += lit("| %1 | %2 | %3 | %4 |\n")
                    .arg(events[i].second)
                    .arg(events[i].first * 1000.0, 0, 'f', 4)
                    .arg(target != ResourceId() ? names.value(target, lit("-")) : lit("-"))
                    .arg(actionNames.value(events[i].second, lit("-")));
    }

    // Attribute each event's GPU time to the resource it renders into.
    QMap<ResourceId, QPair<double, int>> perResource;
    for(const QPair<double, uint32_t> &e : events)
    {
      const ResourceId target = actionOutput.value(e.second, ResourceId());
      if(target == ResourceId())
        continue;
      QPair<double, int> &slot = perResource[target];
      slot.first += e.first;
      slot.second += 1;
    }

    if(!perResource.isEmpty() && total > 0.0)
    {
      QVector<QPair<double, ResourceId>> resRank;
      resRank.reserve(perResource.size());
      for(auto it = perResource.constBegin(); it != perResource.constEnd(); ++it)
        resRank.push_back(qMakePair(it.value().first, it.key()));
      std::sort(resRank.begin(), resRank.end(),
                [](const QPair<double, ResourceId> &a, const QPair<double, ResourceId> &b) {
                  return a.first > b.first;
                });

      const int kTopRes = 10;
      detail += lit("\n按目标资源汇总的 GPU 耗时：\n| 目标资源 | 累计 GPU 毫秒 | 占比 | 事件数 |\n|---|---|---|---|\n");
      for(int i = 0; i < resRank.count() && i < kTopRes; i++)
      {
        const ResourceId id = resRank[i].second;
        detail += lit("| %1 | %2 | %3% | %4 |\n")
                      .arg(names.value(id, lit("-")))
                      .arg(resRank[i].first * 1000.0, 0, 'f', 4)
                      .arg(resRank[i].first / total * 100.0, 0, 'f', 1)
                      .arg(perResource.value(id).second);
      }
    }
  }
  else
  {
    detail += lit("\n- GPU 耗时：当前回放后端不支持 EventGPUDuration 计数器。\n");
  }

  ctrl->Shutdown();

  res.detail = detail;
  return res;
}
}    // anonymous namespace

QString AIAssistant::buildMemoryReport() const
{
  const rdcarray<TextureDescription> &textures = m_Ctx.GetTextures();
  const rdcarray<BufferDescription> &buffers = m_Ctx.GetBuffers();

  uint64_t texTotal = 0;
  for(const TextureDescription &t : textures)
    texTotal += t.byteSize;

  uint64_t bufTotal = 0;
  for(const BufferDescription &b : buffers)
    bufTotal += b.length;

  QString report = lit("## 显存 / 内存\n");
  report += lit("- 纹理数量：%1，合计 %2\n").arg(textures.count()).arg(formatBytes(texTotal));
  report += lit("- 缓冲区数量：%1，合计 %2\n").arg(buffers.count()).arg(formatBytes(bufTotal));
  report += lit("- 资源内存合计：%1\n\n").arg(formatBytes(texTotal + bufTotal));

  // Largest textures
  QVector<int> texIdx;
  texIdx.reserve(textures.count());
  for(int i = 0; i < textures.count(); i++)
    texIdx.push_back(i);
  std::sort(texIdx.begin(), texIdx.end(),
            [&textures](int a, int b) { return textures[a].byteSize > textures[b].byteSize; });

  const int kTopN = 10;
  report += lit("占用最大的纹理：\n");
  report += lit("| 大小 | 尺寸 | 资源 |\n|---|---|---|\n");
  for(int i = 0; i < texIdx.count() && i < kTopN; i++)
  {
    const TextureDescription &t = textures[texIdx[i]];
    QString dims = lit("%1x%2").arg(t.width).arg(t.height);
    if(t.depth > 1)
      dims += lit("x%1").arg(t.depth);
    if(t.arraysize > 1)
      dims += lit("[%1]").arg(t.arraysize);
    if(t.mips > 1)
      dims += lit(" %1 级 mip").arg(t.mips);
    QString name = m_Ctx.GetResourceName(t.resourceId);
    report += lit("| %1 | %2 | %3 |\n").arg(formatBytes(t.byteSize)).arg(dims).arg(name);
  }

  // Largest buffers
  QVector<int> bufIdx;
  bufIdx.reserve(buffers.count());
  for(int i = 0; i < buffers.count(); i++)
    bufIdx.push_back(i);
  std::sort(bufIdx.begin(), bufIdx.end(),
            [&buffers](int a, int b) { return buffers[a].length > buffers[b].length; });

  report += lit("\n占用最大的缓冲区：\n");
  report += lit("| 大小 | 资源 |\n|---|---|\n");
  for(int i = 0; i < bufIdx.count() && i < kTopN; i++)
  {
    const BufferDescription &b = buffers[bufIdx[i]];
    QString name = m_Ctx.GetResourceName(b.resourceId);
    report += lit("| %1 | %2 |\n").arg(formatBytes(b.length)).arg(name);
  }

  return report;
}

QString AIAssistant::buildTimingReportFromResults(const CounterDescription &desc,
                                                  const rdcarray<CounterResult> &results) const
{
  const bool wide = desc.resultByteWidth == 8;
  double total = 0.0;
  QVector<QPair<double, uint32_t>> events;
  events.reserve((int)results.size());
  for(const CounterResult &r : results)
  {
    double secs = wide ? r.value.d : double(r.value.f);
    total += secs;
    events.push_back(qMakePair(secs, r.eventId));
  }
  std::sort(events.begin(), events.end(),
            [](const QPair<double, uint32_t> &a, const QPair<double, uint32_t> &b) {
              return a.first > b.first;
            });

  QString timing = lit("## GPU 耗时\n");
  timing += lit("- 测量事件数：%1\n").arg(events.count());
  timing += lit("- GPU 总耗时（各事件求和）：%1 ms\n").arg(total * 1000.0, 0, 'f', 3);
  if(total > 0.0)
    timing += lit("- 估算 GPU 瓶颈帧率：%1 FPS\n").arg(1.0 / total, 0, 'f', 1);

  const int kTopN = 15;
  timing += lit("\n耗时最高的 %1 个事件：\n").arg(qMin(kTopN, events.count()));
  timing += lit("| EID | GPU 毫秒 | 目标资源 | 事件 |\n|---|---|---|---|\n");
  for(int i = 0; i < events.count() && i < kTopN; i++)
  {
    uint32_t eid = events[i].second;
    const ActionDescription *action = m_Ctx.GetAction(eid);
    QString name = action ? QString(action->GetName(m_Ctx.GetStructuredFile())) : lit("-");
    const ResourceId target = primaryOutputResource(action);
    QString targetName = target != ResourceId() ? m_Ctx.GetResourceName(target) : lit("-");
    timing += lit("| %1 | %2 | %3 | %4 |\n")
                  .arg(eid)
                  .arg(events[i].first * 1000.0, 0, 'f', 4)
                  .arg(targetName)
                  .arg(name);
  }

  // Attribute each event's GPU time to the resource it renders into, so the
  // report shows which resources actually cost the most GPU time.
  QMap<ResourceId, QPair<double, int>> perResource;
  for(const QPair<double, uint32_t> &e : events)
  {
    const ResourceId target = primaryOutputResource(m_Ctx.GetAction(e.second));
    if(target == ResourceId())
      continue;
    QPair<double, int> &slot = perResource[target];
    slot.first += e.first;
    slot.second += 1;
  }

  if(!perResource.isEmpty() && total > 0.0)
  {
    QVector<QPair<double, ResourceId>> resRank;
    resRank.reserve(perResource.size());
    for(auto it = perResource.constBegin(); it != perResource.constEnd(); ++it)
      resRank.push_back(qMakePair(it.value().first, it.key()));
    std::sort(resRank.begin(), resRank.end(),
              [](const QPair<double, ResourceId> &a, const QPair<double, ResourceId> &b) {
                return a.first > b.first;
              });

    const int kTopRes = 15;
    timing += lit("\n按目标资源汇总的 GPU 耗时（把每个事件的耗时归到它渲染的目标）：\n");
    timing += lit("| 目标资源 | 累计 GPU 毫秒 | 占比 | 事件数 |\n|---|---|---|---|\n");
    for(int i = 0; i < resRank.count() && i < kTopRes; i++)
    {
      const ResourceId id = resRank[i].second;
      timing += lit("| %1 | %2 | %3% | %4 |\n")
                    .arg(m_Ctx.GetResourceName(id))
                    .arg(resRank[i].first * 1000.0, 0, 'f', 4)
                    .arg(resRank[i].first / total * 100.0, 0, 'f', 1)
                    .arg(perResource.value(id).second);
    }
  }

  return timing;
}

void AIAssistant::fetchTimingReport(std::function<void(QString)> cont)
{
  if(!m_Ctx.IsCaptureLoaded())
  {
    cont(QString());
    return;
  }

  // GPU timing has to run on the replay thread.
  m_Ctx.Replay().AsyncInvoke([this, cont](IReplayController *controller) {
    const rdcarray<GPUCounter> available = controller->EnumerateCounters();

    if(!available.contains(GPUCounter::EventGPUDuration))
    {
      GUIInvoke::call(this, [cont]() {
        cont(lit("## GPU 耗时\n当前回放后端不支持 GPU 耗时计数器（EventGPUDuration）。"));
      });
      return;
    }

    const CounterDescription desc = controller->DescribeCounter(GPUCounter::EventGPUDuration);
    rdcarray<GPUCounter> want;
    want.push_back(GPUCounter::EventGPUDuration);
    const rdcarray<CounterResult> results = controller->FetchCounters(want);

    GUIInvoke::call(this, [this, cont, desc, results]() {
      cont(buildTimingReportFromResults(desc, results));
    });
  });
}

QString AIAssistant::buildActionSummary() const
{
  const rdcarray<ActionDescription> &roots = m_Ctx.CurRootActions();

  int total = 0, draws = 0, dispatches = 0, clears = 0, copies = 0;

  std::function<void(const rdcarray<ActionDescription> &)> recurse =
      [&](const rdcarray<ActionDescription> &actions) {
        for(const ActionDescription &a : actions)
        {
          total++;
          if(a.flags & ActionFlags::Drawcall)
            draws++;
          if(a.flags & ActionFlags::Dispatch)
            dispatches++;
          if(a.flags & ActionFlags::Clear)
            clears++;
          if(a.flags & ActionFlags::Copy)
            copies++;
          recurse(a.children);
        }
      };
  recurse(roots);

  QString report = lit("## 绘制调用统计\n");
  report += lit("- 事件总数：%1\n").arg(total);
  report += lit("- 绘制调用 (Draw)：%1\n").arg(draws);
  report += lit("- 计算派发 (Dispatch)：%1\n").arg(dispatches);
  report += lit("- 清除 (Clear)：%1\n").arg(clears);
  report += lit("- 拷贝 (Copy)：%1\n").arg(copies);

  report += lit("\n顶层事件：\n");
  int shown = 0;
  const int kMaxTop = 30;
  for(const ActionDescription &a : roots)
  {
    QString name = a.GetName(m_Ctx.GetStructuredFile());
    report += lit("- EID %1: %2\n").arg(a.eventId).arg(name);
    if(++shown >= kMaxTop)
    {
      report += lit("- ...\n");
      break;
    }
  }

  return report;
}

void AIAssistant::on_analyzeButton_clicked()
{
  if(m_Busy)
    return;

  if(!m_Ctx.IsCaptureLoaded())
  {
    appendMessage(lit("system"),
                  lit("当前没有加载抓帧文件，请先在 RenderDoc 中打开一个抓帧。"), true);
    return;
  }

  appendMessage(lit("system"), lit("正在分析抓帧的显存占用与 GPU 耗时……"), true);

  const QString memoryReport = buildMemoryReport();

  fetchTimingReport(
      [this, memoryReport](QString timing) { presentAnalysis(memoryReport, timing); });
}

void AIAssistant::on_skillButton_clicked()
{
  if(m_Busy)
    return;

  if(!m_Ctx.IsCaptureLoaded())
  {
    appendMessage(lit("system"),
                  lit("当前没有加载抓帧文件，请先在 RenderDoc 中打开一个抓帧。"), true);
    return;
  }

  const QString path = QFileDialog::getOpenFileName(
      this, lit("选择本地 Skill 文件"), m_LastSkillDir,
      lit("Skill 文件 (*.md *.markdown *.txt);;所有文件 (*.*)"));

  if(path.isEmpty())
    return;

  QFile f(path);
  if(!f.open(QIODevice::ReadOnly | QIODevice::Text))
  {
    appendMessage(lit("system"), lit("无法读取 Skill 文件：%1").arg(path.toHtmlEscaped()), true);
    return;
  }
  const QString raw = QString::fromUtf8(f.readAll());
  f.close();

  m_LastSkillDir = QFileInfo(path).absolutePath();
  saveSettings();

  // Parse optional YAML front matter ("---\n...\n---") to pick up a friendly name.
  QString name = QFileInfo(path).completeBaseName();
  QString body = raw;
  if(raw.startsWith(lit("---")))
  {
    const int firstNl = raw.indexOf(QLatin1Char('\n'));
    const int endFence = firstNl >= 0 ? raw.indexOf(lit("\n---"), firstNl) : -1;
    if(firstNl >= 0 && endFence > firstNl)
    {
      const QString front = raw.mid(firstNl + 1, endFence - firstNl - 1);
      const int bodyStart = raw.indexOf(QLatin1Char('\n'), endFence + 1);
      body = (bodyStart >= 0) ? raw.mid(bodyStart + 1) : QString();

      const QStringList lines = front.split(QLatin1Char('\n'));
      for(const QString &raw_line : lines)
      {
        const QString line = raw_line.trimmed();
        if(line.startsWith(lit("name:"), Qt::CaseInsensitive))
          name = line.mid(5).trimmed();
      }
    }
  }

  body = body.trimmed();
  if(body.isEmpty())
  {
    appendMessage(lit("system"), lit("Skill 文件内容为空，无法进行分析。"), true);
    return;
  }

  m_SkillName = name;
  m_SkillInstructions = body;

  appendMessage(lit("system"),
                lit("已导入 Skill「<b>%1</b>」，正在采集抓帧数据并按该 Skill 进行分析……")
                    .arg(name.toHtmlEscaped()),
                true);

  const QString memoryReport = buildMemoryReport();
  fetchTimingReport(
      [this, memoryReport](QString timing) { presentAnalysis(memoryReport, timing); });
}

void AIAssistant::on_folderButton_clicked()
{
  if(m_Busy || m_BatchRunning)
    return;

  const QString dir = QFileDialog::getExistingDirectory(
      this, lit("选择包含 .rdc 抓帧文件的文件夹"), m_LastBatchDir);
  if(dir.isEmpty())
    return;

  m_LastBatchDir = dir;
  saveSettings();

  QStringList files;
  QDirIterator it(dir, QStringList() << lit("*.rdc"), QDir::Files, QDirIterator::Subdirectories);
  while(it.hasNext())
    files << it.next();
  files.sort(Qt::CaseInsensitive);

  if(files.isEmpty())
  {
    appendMessage(lit("system"), lit("在该文件夹（含子目录）中没有找到 .rdc 文件。"), true);
    return;
  }

  const int kMaxFiles = 40;
  bool truncated = false;
  if(files.size() > kMaxFiles)
  {
    files = files.mid(0, kMaxFiles);
    truncated = true;
  }

  appendMessage(lit("system"),
                lit("找到 %1 个 .rdc 文件%2，正在逐个打开回放并分析，请稍候……")
                    .arg(files.size())
                    .arg(truncated ? lit("（数量较多，仅分析前 %1 个）").arg(kMaxFiles) : QString()),
                true);

  m_BatchRunning = true;
  setBusy(true);

  const QString folder = dir;
  LambdaThread *th = new LambdaThread([this, files, folder]() {
    QVector<BatchFileResult> results;
    for(int i = 0; i < files.size(); i++)
    {
      const QString path = files[i];
      const QString name = QFileInfo(path).fileName();
      const int idx = i;
      const int total = files.size();
      GUIInvoke::call(this, [this, idx, total, name]() {
        ui->executeLabel->setText(lit("正在分析 %1/%2：%3").arg(idx + 1).arg(total).arg(name));
      });
      results.push_back(analyzeCaptureFile(path));
    }

    // Assemble the combined markdown report (pure string work, safe off the UI thread).
    QString report = lit("# 批量抓帧分析报告\n");
    report += lit("分析文件夹：%1\n").arg(folder);
    report += lit("抓帧文件数：%1\n\n").arg(results.size());

    report += lit("## 概览\n");
    report += lit("| 文件 | API | 纹理数 | 显存合计 | GPU 总耗时(ms) | 估算 FPS |\n");
    report += lit("|---|---|---|---|---|---|\n");
    for(const BatchFileResult &r : results)
    {
      if(!r.error.isEmpty())
      {
        report += lit("| %1 | - | - | - | 失败 | - |\n").arg(r.fileName);
        continue;
      }
      const QString ms =
          r.hasTiming ? QString::number(r.gpuTotalSecs * 1000.0, 'f', 3) : lit("N/A");
      const QString fps = (r.hasTiming && r.gpuTotalSecs > 0.0)
                              ? QString::number(1.0 / r.gpuTotalSecs, 'f', 1)
                              : lit("N/A");
      report += lit("| %1 | %2 | %3 | %4 | %5 | %6 |\n")
                    .arg(r.fileName)
                    .arg(r.api)
                    .arg(r.texCount)
                    .arg(formatBytes(r.texTotal + r.bufTotal))
                    .arg(ms)
                    .arg(fps);
    }

    report += lit("\n## 各文件详情\n");
    for(const BatchFileResult &r : results)
    {
      if(!r.error.isEmpty())
      {
        report += lit("\n### 文件：%1\n- 分析失败：%2\n").arg(r.fileName, r.error);
        continue;
      }
      report += lit("\n") + r.detail;
    }

    const int count = results.size();
    GUIInvoke::call(this, [this, report, count]() { presentBatchAnalysis(report, count); });
  });
  th->selfDelete(true);
  th->setName(lit("BatchAnalysis"));
  th->start();
}

QString AIAssistant::exportHtmlReport(const QString &reportMarkdown)
{
  QString doc = lit(
      "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
      "<meta name='viewport' content='width=device-width, initial-scale=1'>"
      "<title>RenderDoc 抓帧分析报告</title><style>"
      "*{box-sizing:border-box;}"
      "body{font-family:'Segoe UI','Microsoft YaHei UI','PingFang SC',sans-serif;"
      "background:#17171a;color:#e6e6e6;line-height:175%;margin:0;padding:32px 16px;"
      "font-size:15px;}"
      ".wrap{max-width:940px;margin:0 auto;background:#1e1e22;border:1px solid #2c2c30;"
      "border-radius:14px;padding:32px 40px 40px;box-shadow:0 8px 30px rgba(0,0,0,.35);}"
      ".hdr{border-bottom:2px solid #2f7fe0;padding-bottom:14px;margin-bottom:8px;}"
      ".hdr .title{font-size:24px;font-weight:700;color:#6ea8fe;}"
      ".hdr .sub{color:#8a8a90;font-size:13px;margin-top:4px;}"
      "h1{color:#6ea8fe;font-size:22px;}"
      "h2{color:#f0f0f0;font-size:19px;border-bottom:1px solid #2c2c30;padding-bottom:6px;"
      "margin:28px 0 10px;}"
      "h3{color:#eaeaea;font-size:16px;margin:20px 0 8px;}"
      "h4{color:#dcdce2;font-size:14px;margin:16px 0 6px;}"
      "p{margin:10px 0;line-height:175%;}"
      "ul,ol{margin:10px 0;padding-left:26px;}"
      "li{margin:5px 0;line-height:175%;}"
      "blockquote{margin:12px 0;padding:8px 16px;border-left:4px solid #3d8bfd;"
      "background:#20232a;color:#c7c7cf;border-radius:0 6px 6px 0;}"
      "hr{border:none;border-top:1px solid #33343a;margin:22px 0;}"
      "code{background:#2b2b30;color:#e6c07b;padding:2px 6px;border-radius:5px;"
      "font-family:'Cascadia Mono','Consolas',monospace;font-size:90%;}"
      "pre{background:#111113;border:1px solid #2c2c30;padding:14px 16px;border-radius:10px;"
      "overflow:auto;margin:12px 0;}"
      "pre code{background:transparent;color:#dcdcdc;padding:0;font-size:13px;}"
      "table{border-collapse:collapse;margin:14px 0;width:100%;font-size:14px;"
      "border:1px solid #35353b;border-radius:8px;overflow:hidden;}"
      "th{background:#2a2a30;text-align:left;font-weight:600;color:#f0f0f0;}"
      "td,th{border-bottom:1px solid #35353b;padding:8px 12px;}"
      "tr:last-child td{border-bottom:none;}"
      "tbody tr:nth-child(even) td,table tr:nth-child(even) td{background:#232327;}"
      "a{color:#6ea8fe;text-decoration:none;} a:hover{text-decoration:underline;}"
      ".footer{color:#8a8a90;font-size:12px;margin-top:34px;border-top:1px solid #2c2c30;"
      "padding-top:12px;}"
      "</style></head><body><div class='wrap'>");
  doc += lit("<div class='hdr'><div class='title'>RenderDoc 抓帧分析报告</div>"
             "<div class='sub'>由 RenderDoc AI 助手 (CodeBuddy) 生成 · %1</div></div>")
             .arg(QDateTime::currentDateTime().toString(lit("yyyy-MM-dd HH:mm:ss")));

  // The banner already shows the document title, so drop a redundant leading H1/H2
  // ("# 抓帧分析报告") to avoid showing the same title twice.
  QString body = reportMarkdown;
  if(body.startsWith(lit("# ")) || body.startsWith(lit("## ")))
  {
    int nl = body.indexOf(QLatin1Char('\n'));
    body = (nl >= 0) ? body.mid(nl + 1) : QString();
  }
  while(body.startsWith(QLatin1Char('\n')))
    body.remove(0, 1);

  doc += markdownishToHtml(body);
  doc += lit("<div class='footer'>本报告由 RenderDoc AI 助手自动生成，仅供性能与内存优化参考。</div>");
  doc += lit("</div></body></html>");

  QString baseName = lit("renderdoc_analysis");
  QString dir;
  if(m_Ctx.IsCaptureLoaded())
  {
    QFileInfo fi(QString(m_Ctx.GetCaptureFilename()));
    if(!fi.completeBaseName().isEmpty())
      baseName = fi.completeBaseName() + lit("_analysis");
    if(fi.absoluteDir().exists())
      dir = fi.absolutePath();
  }
  if(dir.isEmpty())
    dir = QDir::tempPath();

  const QString stamp = QDateTime::currentDateTime().toString(lit("yyyyMMdd_hhmmss"));
  const QString path = QDir(dir).filePath(lit("%1_%2.html").arg(baseName, stamp));

  QFile f(path);
  if(!f.open(QIODevice::WriteOnly | QIODevice::Text))
    return QString();

  QTextStream ts(&f);
  ts.setCodec("UTF-8");
  ts << doc;
  f.close();

  return path;
}

void AIAssistant::doExport(const QString &reportMarkdown)
{
  const QString path = exportHtmlReport(reportMarkdown);
  if(path.isEmpty())
  {
    appendMessage(lit("system"), lit("导出 HTML 报告失败。"), true);
    return;
  }

  const QString url = QUrl::fromLocalFile(path).toString();
  appendMessage(lit("system"),
                lit("报告已导出：<a href=\"%1\">%2</a>").arg(url, path.toHtmlEscaped()), true);
  QDesktopServices::openUrl(QUrl::fromLocalFile(path));
}

void AIAssistant::presentAnalysis(const QString &memoryReport, const QString &timingReport)
{
  // A skill is a one-shot: consume it here so a subsequent plain "分析" won't reuse it.
  const QString skillName = m_SkillName;
  const QString skillInstructions = m_SkillInstructions;
  m_SkillName.clear();
  m_SkillInstructions.clear();

  QString report = lit("# 抓帧分析报告\n");
  report += lit("抓帧文件：%1\n").arg(QString(m_Ctx.GetCaptureFilename()));
  report += lit("图形 API：%1\n").arg(ToQStr(m_Ctx.APIProps().pipelineType));
  if(!skillName.isEmpty())
    report += lit("分析 Skill：%1\n").arg(skillName);
  report += lit("\n");
  report += memoryReport + lit("\n") + timingReport;

  QString suffix;
  if(!skillInstructions.isEmpty())
  {
    suffix = lit("\n\n以下是用户提供的分析 Skill（名称：%1）。请严格按照该 Skill 的指引，"
                 "结合上面的 RenderDoc 抓帧分析数据（显存/内存占用与逐事件 GPU 耗时）进行分析，"
                 "输出结构化的分析结论与按优先级排序的优化建议，并全程使用中文回答。\n\n"
                 "===== Skill 指引开始 =====\n%2\n===== Skill 指引结束 =====")
                 .arg(skillName, skillInstructions);
  }
  else
  {
    suffix = lit("\n\n你是一名图形性能优化工程师。请基于上面的 RenderDoc 抓帧分析数据"
                 "（显存/内存占用以及逐事件的 GPU 耗时），找出最可能的性能与内存瓶颈，"
                 "并给出具体的、按优先级排序的优化建议。请全程使用中文回答。");
  }

  finalizeReport(report, report + suffix);
}

void AIAssistant::finalizeReport(const QString &reportMarkdown, const QString &aiPrompt)
{
  appendMessage(lit("assistant"), reportMarkdown, false);

  if(!m_Connected)
  {
    appendMessage(lit("system"),
                  lit("未连接到 CodeBuddy，仅显示本地分析结果。点击“重新连接”可获取 AI 优化建议。"),
                  true);
    doExport(reportMarkdown);
    return;
  }

  // Defer the export until the AI reply arrives so the report includes its suggestions.
  m_PendingAnalysisReport = reportMarkdown;
  m_AnalysisAwaitingAI = true;
  startRun(aiPrompt);
}

void AIAssistant::presentBatchAnalysis(const QString &report, int fileCount)
{
  m_BatchRunning = false;
  setBusy(false);

  appendMessage(lit("assistant"), report, false);

  if(!m_Connected)
  {
    appendMessage(
        lit("system"),
        lit("未连接到 CodeBuddy，仅显示本地批量分析结果。点击“重新连接”可获取 AI 优化建议。"), true);
    doExport(report);
    return;
  }

  // Defer the export until the AI reply arrives so the report includes its suggestions.
  m_PendingAnalysisReport = report;
  m_AnalysisAwaitingAI = true;

  QString prompt = report;
  prompt += lit("\n\n你是一名图形性能优化工程师。以上是一个文件夹中 %1 个 RenderDoc 抓帧的批量分析数据"
                "（每个文件的显存/内存占用与逐事件 GPU 耗时）。请对比这些抓帧，找出共性的性能与内存瓶颈、"
                "各文件之间的差异与异常项，并给出按优先级排序的整体优化建议。请全程使用中文回答。")
                .arg(fileCount);
  startRun(prompt);
}

void AIAssistant::startRun(const QString &prompt)
{
  if(m_AcpSessionId.isEmpty())
  {
    finishRun(lit("尚未建立 CodeBuddy 会话，请点击“重新连接”。"));
    return;
  }

  setBusy(true);
  m_AssistantBuffer.clear();
  m_AssistantOpen = false;
  m_StreamBuffer.clear();
  m_SseEventType.clear();
  m_SseData.clear();

  ui->executeLabel->setText(lit("正在发送到 CodeBuddy（%1）……").arg(ui->modelCombo->currentText()));

  QJsonObject textPart;
  textPart[lit("type")] = lit("text");
  textPart[lit("text")] = prompt;

  QJsonArray promptArr;
  promptArr.append(textPart);

  QJsonObject params;
  params[lit("sessionId")] = m_AcpSessionId;
  params[lit("prompt")] = promptArr;

  QNetworkRequest req = makeAcpRequest(lit("/api/v1/acp"));
  m_StreamReply = m_Net->post(req, rpcBody(lit("session/prompt"), params));
  QObject::connect(m_StreamReply, &QNetworkReply::readyRead, this, &AIAssistant::streamReadyRead);
  QObject::connect(m_StreamReply, &QNetworkReply::finished, this, &AIAssistant::streamFinished);
}

// Pull the text out of an ACP agent_message_chunk content payload.
static QString acpChunkText(const QJsonValue &content)
{
  if(content.isObject())
    return content.toObject().value(lit("text")).toString();
  if(content.isString())
    return content.toString();
  if(content.isArray())
  {
    QString s;
    for(const QJsonValue &v : content.toArray())
      s += v.toObject().value(lit("text")).toString();
    return s;
  }
  return QString();
}

void AIAssistant::handleSseEvent(const QString &eventType, const QString &eventData)
{
  Q_UNUSED(eventType);

  if(eventData.isEmpty() || eventData == lit("[DONE]") || eventData == lit("{}") ||
     eventData == lit(":ok"))
    return;

  QJsonParseError err;
  QJsonDocument doc = QJsonDocument::fromJson(eventData.toUtf8(), &err);
  if(err.error != QJsonParseError::NoError || !doc.isObject())
    return;

  const QJsonObject obj = doc.object();

  // ACP streaming notification: session/update -> agent_message_chunk carries the text deltas.
  if(obj.value(lit("method")).toString() == lit("session/update"))
  {
    const QJsonObject update = obj.value(lit("params")).toObject().value(lit("update")).toObject();
    if(update.value(lit("sessionUpdate")).toString() == lit("agent_message_chunk"))
    {
      const QString text = acpChunkText(update.value(lit("content")));
      if(!text.isEmpty())
        appendAssistantDelta(text);
    }
    return;
  }

  // JSON-RPC error response
  if(obj.value(lit("error")).isObject())
  {
    const QString msg = obj.value(lit("error")).toObject().value(lit("message")).toString();
    if(!msg.isEmpty())
      appendAssistantDelta(lit("\n\n[CodeBuddy error] ") + msg);
  }
}

void AIAssistant::processSseBuffer()
{
  int idx;
  while((idx = m_StreamBuffer.indexOf(QLatin1Char('\n'))) >= 0)
  {
    QString line = m_StreamBuffer.left(idx);
    m_StreamBuffer.remove(0, idx + 1);

    if(line.endsWith(QLatin1Char('\r')))
      line.chop(1);

    // Blank line dispatches one SSE event
    if(line.isEmpty())
    {
      if(!m_SseData.isEmpty() || !m_SseEventType.isEmpty())
        handleSseEvent(m_SseEventType, m_SseData.trimmed());
      m_SseEventType.clear();
      m_SseData.clear();
      continue;
    }

    if(line.startsWith(QLatin1Char(':')))
      continue;

    if(line.startsWith(lit("event:"), Qt::CaseInsensitive))
    {
      m_SseEventType = line.mid(6).trimmed();
      continue;
    }

    if(line.startsWith(lit("data:"), Qt::CaseInsensitive))
    {
      QString payload = line.mid(5);
      if(payload.startsWith(QLatin1Char(' ')))
        payload.remove(0, 1);
      if(!m_SseData.isEmpty())
        m_SseData += QLatin1Char('\n');
      m_SseData += payload;
      continue;
    }
  }
}

void AIAssistant::streamReadyRead()
{
  if(!m_StreamReply)
    return;

  m_StreamBuffer += QString::fromUtf8(m_StreamReply->readAll());
  processSseBuffer();
}

void AIAssistant::streamFinished()
{
  QNetworkReply *reply = m_StreamReply;
  if(!reply)
    return;

  // drain remaining while reply is still current
  streamReadyRead();
  if(!m_SseData.isEmpty() || !m_SseEventType.isEmpty())
  {
    handleSseEvent(m_SseEventType, m_SseData.trimmed());
    m_SseEventType.clear();
    m_SseData.clear();
  }
  m_StreamReply = NULL;

  QString err;
  if(reply->error() != QNetworkReply::NoError &&
     reply->error() != QNetworkReply::OperationCanceledError)
    err = reply->errorString();

  reply->deleteLater();

  if(m_AssistantBuffer.isEmpty() && err.isEmpty())
    appendAssistantDelta(lit("（未收到任何响应）"));

  finishRun(err);
}

void AIAssistant::cancelCurrentRun()
{
  if(!m_Busy)
    return;

  if(!m_AcpSessionId.isEmpty())
  {
    QJsonObject params;
    params[lit("sessionId")] = m_AcpSessionId;
    QNetworkRequest req = makeAcpRequest(lit("/api/v1/acp"));
    QNetworkReply *cancelReply = m_Net->post(req, rpcBody(lit("session/cancel"), params));
    QObject::connect(cancelReply, &QNetworkReply::finished, cancelReply, &QNetworkReply::deleteLater);
  }

  if(m_StreamReply)
  {
    QNetworkReply *r = m_StreamReply;
    m_StreamReply = NULL;
    r->disconnect(this);
    r->abort();
    r->deleteLater();
  }

  appendMessage(lit("system"), lit("已取消本次请求。"), true);
  finishRun();
}

void AIAssistant::finishRun(const QString &error)
{
  if(!m_Busy && error.isEmpty())
    return;

  if(!error.isEmpty())
    appendMessage(lit("system"), escapeHtml(error), true);

  // If this run was the analysis request, export the report now that we have the AI reply.
  if(m_AnalysisAwaitingAI)
  {
    m_AnalysisAwaitingAI = false;
    QString full = m_PendingAnalysisReport;
    m_PendingAnalysisReport.clear();

    const QString ai = m_AssistantBuffer.trimmed();
    if(!ai.isEmpty())
      full += lit("\n\n## AI 优化建议\n") + ai;

    doExport(full);
  }

  m_Busy = false;
  m_AssistantOpen = false;
  m_AssistantBuffer.clear();
  setBusy(false);
  ui->executeLabel->setText(m_Connected ? lit("就绪 - %1").arg(m_BaseUrl) : lit("未连接"));
}
