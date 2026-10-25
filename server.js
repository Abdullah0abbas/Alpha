const http = require('http');

const server = http.createServer((req, res) => {
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
  res.end(`<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>P2P Telegram Bot</title>
  <style>
    body {
      margin: 0;
      padding: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      background: #090d16;
      color: #f1f5f9;
      font-family: system-ui, -apple-system, sans-serif;
    }
    .card {
      background: #131b2e;
      border: 1px solid #1e293b;
      border-radius: 16px;
      padding: 32px 40px;
      text-align: center;
      max-width: 420px;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.4);
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: rgba(34, 197, 94, 0.15);
      color: #4ade80;
      padding: 6px 14px;
      border-radius: 9999px;
      font-size: 13px;
      font-weight: 600;
      margin-bottom: 20px;
    }
    .pulse {
      width: 8px;
      height: 8px;
      background: #22c55e;
      border-radius: 50%;
      box-shadow: 0 0 8px #22c55e;
    }
    h1 {
      margin: 0 0 10px 0;
      font-size: 22px;
      font-weight: 700;
    }
    p {
      margin: 0;
      color: #94a3b8;
      font-size: 14px;
      line-height: 1.6;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">
      <span class="pulse"></span>
      الخدمة تعمل بنجاح (Healthy)
    </div>
    <h1>🤖 P2P Telegram Bot</h1>
    <p>السيرفر يستجيب بشكل طبيعي. لا توجد أي أخطاء، ونافذة المعاينة متصلة بالخدمة بنجاح.</p>
  </div>
</body>
</html>`);
});

const PORT = 3000;
server.listen(PORT, '0.0.0.0', () => {
  console.log(`P2P Bot health server listening on port ${PORT}`);
});
