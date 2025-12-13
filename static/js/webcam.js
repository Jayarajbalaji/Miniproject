// Simple webcam capture used by register & capture_face templates.
// Expects: <video id="video">, <canvas id="canvas">, <button id="captureBtn">, hidden input id="face_image", <div id="preview">

(async function(){
  const video = document.getElementById("video");
  const canvas = document.getElementById("canvas");
  const captureBtn = document.getElementById("captureBtn");
  const face_input = document.getElementById("face_image");
  const preview = document.getElementById("preview");

  if (!video) return;

  // Normalize capture size to keep payload small (helps avoid 413 errors)
  const TARGET_WIDTH = 320;
  const TARGET_HEIGHT = 240;
  if (canvas) {
    canvas.width = TARGET_WIDTH;
    canvas.height = TARGET_HEIGHT;
  }

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
    video.srcObject = stream;
  } catch (e) {
    console.error("getUserMedia error:", e);
    alert("Unable to access camera.");
    return;
  }

  const form = captureBtn ? (captureBtn.closest("form") || document.querySelector("form")) : document.querySelector("form");
  const purpose = form ? (form.dataset && form.dataset.purpose) : null;

  // Manual capture button (used for registration and non-auto flows)
  if (captureBtn) {
    captureBtn.addEventListener("click", () => {
      const ctx = canvas.getContext("2d");
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      // Use compressed JPEG instead of PNG to significantly reduce payload size
      const dataUrl = canvas.toDataURL("image/jpeg", 0.7);
      face_input.value = dataUrl;
      preview.innerHTML = `<img src="${dataUrl}" width="160">`;
    });
  }

  // Auto behaviour for login: capture once and submit automatically
  if (form && purpose === "login") {
    // Give the camera a short time to adjust exposure/focus
    setTimeout(() => {
      const ctx = canvas.getContext("2d");
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const dataUrl = canvas.toDataURL("image/jpeg", 0.7);
      face_input.value = dataUrl;
      preview.innerHTML = `<img src="${dataUrl}" width="160">`;
      form.submit();
    }, 1500);
  } else if (form) {
    // For other purposes, auto-capture on submit if user forgot to press Capture
    form.addEventListener("submit", () => {
      if (!face_input.value) {
        const ctx = canvas.getContext("2d");
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        const dataUrl = canvas.toDataURL("image/jpeg", 0.7);
        face_input.value = dataUrl;
        preview.innerHTML = `<img src="${dataUrl}" width="160">`;
      }
    });
  }
})();
