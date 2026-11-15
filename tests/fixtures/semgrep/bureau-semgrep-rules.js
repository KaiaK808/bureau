import child_process from "child_process";

// ruleid: bureau.javascript.exec-command
child_process.exec(`echo ${userText}`);
// ruleid: bureau.javascript.exec-command
child_process.execSync("echo " + userText);
// ok: bureau.javascript.exec-command
child_process.exec("echo hello");
// ok: bureau.javascript.exec-command
child_process.execFile("echo", [userText]);
// ruleid: bureau.javascript.dynamic-code
eval(userText);
// ruleid: bureau.javascript.dynamic-code
new Function(userText);
// ok: bureau.javascript.dynamic-code
JSON.parse(userText);
// ruleid: bureau.javascript.reject-unauthorized
const unsafeOptions = {rejectUnauthorized: false};
// ok: bureau.javascript.reject-unauthorized
const safeOptions = {rejectUnauthorized: true};
// ruleid: bureau.javascript.node-tls
process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";
// ok: bureau.javascript.node-tls
process.env.NODE_TLS_REJECT_UNAUTHORIZED = "1";
