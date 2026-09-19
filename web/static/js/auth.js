/* 登录凭证只由浏览器保存为 HttpOnly Cookie。 */
const authForm=document.querySelector('#auth-form');
if(authForm)authForm.onsubmit=async event=>{
  // 1. 阻止表单整页提交，读取登录或注册模式并检查两次密码是否一致。
  event.preventDefault();
  const mode=document.body.dataset.auth, button=document.querySelector('#auth-submit'), error=document.querySelector('#auth-error');
  const body=Object.fromEntries(new FormData(authForm));
  if(mode==='register' && body.password!==body.password_confirm){error.textContent='两次密码不一致。';error.hidden=false;return;}
  // 2. 提交期间禁用按钮，凭证 Cookie 由后端设置，前端不保存密码或登录令牌。
  button.disabled=true;error.hidden=true;
  try{
    const response=await fetch(`/auth/${mode}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'请检查用户名和密码格式。');
    if(mode==='register'){location.replace('/login.html?registered=1');return;}
    // 3. 登录成功后通知其他标签页清除旧账号状态；这里只写变化标记，不写凭证。
    localStorage.setItem('zhiku.auth-change',crypto.randomUUID());
    location.replace('/chat.html');
  }catch(e){error.textContent=e.message;error.hidden=false;button.disabled=false;}
};
if(new URLSearchParams(location.search).has('registered')){const notice=document.querySelector('.muted');notice.textContent='注册成功，请使用新账号登录。';}
