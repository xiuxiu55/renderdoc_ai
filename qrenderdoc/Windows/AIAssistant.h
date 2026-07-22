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

#pragma once

#include <functional>
#include <QFrame>
#include <QNetworkRequest>
#include <QPointer>
#include <QTimer>
#include "Code/Interface/QRDInterface.h"

namespace Ui
{
class AIAssistant;
}

class QNetworkAccessManager;
class QNetworkReply;
class QKeyEvent;
class QJsonObject;

class AIAssistant : public QFrame, public IAIAssistant, public ICaptureViewer
{
  Q_OBJECT

public:
  explicit AIAssistant(ICaptureContext &ctx, QWidget *parent = 0);
  ~AIAssistant();

  // IAIAssistant
  QWidget *Widget() override { return this; }

  // ICaptureViewer
  void OnCaptureLoaded() override;
  void OnCaptureClosed() override;
  void OnSelectedEventChanged(uint32_t eventId) override {}
  void OnEventChanged(uint32_t eventId) override;

private slots:
  void on_sendButton_clicked();
  void on_cancelButton_clicked();
  void on_reconnectButton_clicked();
  void on_settingsButton_clicked();
  void on_analyzeButton_clicked();
  void on_skillButton_clicked();
  void on_folderButton_clicked();
  void onModelSelected(int index);
  void healthCheckFinished();
  void acpConnectFinished();
  void acpInitFinished();
  void acpNewSessionFinished();
  void streamReadyRead();
  void streamFinished();

private:
  bool eventFilter(QObject *watched, QEvent *event) override;

  void applyTheme();
  void loadSettings();
  void saveSettings();
  void setConnected(bool connected, const QString &detail = QString());
  void setBusy(bool busy);
  void appendMessage(const QString &role, const QString &htmlOrText, bool asHtml = false);
  void appendAssistantDelta(const QString &text);
  void refreshChatHtml();
  QString escapeHtml(const QString &text) const;
  QString markdownishToHtml(const QString &text) const;
  QString buildCaptureContext() const;
  QString buildMemoryReport() const;
  QString buildActionSummary() const;
  QString buildTimingReportFromResults(const CounterDescription &desc,
                                       const rdcarray<CounterResult> &results) const;
  void fetchTimingReport(std::function<void(QString)> cont);
  void presentAnalysis(const QString &memoryReport, const QString &timingReport);
  void presentBatchAnalysis(const QString &report, int fileCount);
  void finalizeReport(const QString &reportMarkdown, const QString &aiPrompt);
  QString exportHtmlReport(const QString &reportMarkdown);
  void doExport(const QString &reportMarkdown);
  bool questionNeedsTiming(const QString &q) const;
  bool questionNeedsMemory(const QString &q) const;
  bool questionNeedsActions(const QString &q) const;
  void sendQuestion(const QString &userText);
  QString assembleQuestionPrompt(const QString &userText, const QString &dataContext) const;
  QString formatNetworkError(QNetworkReply *reply) const;
  QNetworkRequest makeRequest(const QString &path) const;
  QNetworkRequest makeAcpRequest(const QString &path) const;
  QByteArray rpcBody(const QString &method, const QJsonObject &params);
  void connectToCodeBuddy();
  void startAcpHandshake();
  void populateModels(const QVector<QPair<QString, QString>> &models, const QString &current);
  void applyModelSelection(const QString &modelId);
  void startRun(const QString &prompt);
  void cancelCurrentRun();
  void finishRun(const QString &error = QString());
  void processSseBuffer();
  void handleSseEvent(const QString &eventType, const QString &eventData);

  Ui::AIAssistant *ui;
  ICaptureContext &m_Ctx;

  QNetworkAccessManager *m_Net = NULL;
  QPointer<QNetworkReply> m_HealthReply;
  QPointer<QNetworkReply> m_AcpReply;
  QPointer<QNetworkReply> m_StreamReply;

  QTimer m_HealthTimer;

  QString m_BaseUrl;
  QString m_AuthToken;
  QString m_Model;
  QString m_ConnectionId;
  QString m_SessionToken;
  QString m_AcpSessionId;
  int m_RpcId = 0;
  QString m_PendingAnalysisReport;
  QString m_SkillName;
  QString m_SkillInstructions;
  QString m_LastSkillDir;
  QString m_LastBatchDir;
  bool m_BatchRunning = false;
  QString m_StreamBuffer;
  QString m_SseEventType;
  QString m_SseData;
  QString m_AssistantBuffer;

  struct ChatMessage
  {
    QString role;
    QString content;
    bool html = false;
  };
  QVector<ChatMessage> m_History;

  bool m_Connected = false;
  bool m_Busy = false;
  bool m_AssistantOpen = false;
  bool m_AcpReady = false;
  bool m_AcpConnecting = false;
  bool m_PopulatingModels = false;
  bool m_AnalysisAwaitingAI = false;
};
