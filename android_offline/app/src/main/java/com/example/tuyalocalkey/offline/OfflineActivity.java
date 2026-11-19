package com.example.tuyalocalkey.offline;

import android.app.Activity;
import android.content.ContentValues;
import android.content.Intent;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;
import android.util.Base64;
import android.util.Log;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.TextView;
import android.widget.Toast;

import com.chaquo.python.android.AndroidPlatform;
import com.chaquo.python.Python;

import java.io.OutputStream;
import java.net.Inet4Address;
import java.net.InetSocketAddress;
import java.net.Socket;

/** Runs the full Python app locally and shows it in a WebView. */
public class OfflineActivity extends Activity {
    private static final String TAG = "TuyaOffline";
    private static final String LOCAL_URL = "http://127.0.0.1:8000/local";

    private WebView web;
    private TextView status;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private int attempts = 0;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_offline);

        status = findViewById(R.id.status);
        web = findViewById(R.id.webview);

        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setUseWideViewPort(true);
        settings.setLoadWithOverviewMode(true);
        settings.setBuiltInZoomControls(true);
        settings.setDisplayZoomControls(false);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);

        web.setWebViewClient(new WebViewClient() {
            @SuppressWarnings("deprecation")
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                view.loadUrl(url);
                return true;
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                view.loadUrl(request.getUrl().toString());
                return true;
            }
        });

        // Lets the page save the login QR straight to the phone's Gallery, so a
        // single phone can scan it from Smart Life's Gallery picker.
        web.addJavascriptInterface(new QrSaver(), "TuyaNative");

        web.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent, String disposition,
                                        String mimeType, long contentLength) {
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                } catch (Exception e) {
                    Toast.makeText(OfflineActivity.this, "No app can open this download", Toast.LENGTH_SHORT).show();
                }
            }
        });

        startPython();
        waitForServer();
    }

    private void startPython() {
        Log.i(TAG, "Starting embedded Python server");
        Thread thread = new Thread(() -> {
            try {
                if (!Python.isStarted()) {
                    Python.start(new AndroidPlatform(getApplicationContext()));
                }
                String hint = networkHint();
                // Remote access is handled by Home Assistant ingress / Tailscale
                // add-on, so the phone's own server stays on localhost.
                Log.i(TAG, "Wi-Fi subnet hint: " + (hint.isEmpty() ? "none" : hint));
                Python.getInstance()
                       .getModule("android_entry")
                       .callAttr("start", getFilesDir().getAbsolutePath(), "127.0.0.1", 8000, hint);
            } catch (final Throwable e) {
                Log.e(TAG, "Embedded Python server failed", e);
                handler.post(() -> status.setText("Server error: " + e));
            }
        }, "tuya-python");
        thread.setDaemon(true);
        thread.start();
    }

    /** The phone's Wi-Fi /24, so Python can scan without any typed subnet. */
    private String networkHint() {
        try {
            ConnectivityManager manager =
                    (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
            if (manager == null) return "";
            Network[] networks = manager.getAllNetworks();
            if (networks == null) return "";
            for (Network network : networks) {
                NetworkCapabilities caps = manager.getNetworkCapabilities(network);
                if (caps == null || !caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) continue;
                LinkProperties links = manager.getLinkProperties(network);
                if (links == null) continue;
                for (LinkAddress address : links.getLinkAddresses()) {
                    if (!(address.getAddress() instanceof Inet4Address)) continue;
                    String ip = address.getAddress().getHostAddress();
                    if (ip == null || ip.equals("0.0.0.0") || ip.startsWith("127.")) continue;
                    // /24 keeps the scan inside the app's MAX_ADDRESSES limit.
                    return ip + "/24";
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "Could not detect Wi-Fi subnet", e);
        }
        return "";
    }

    private void waitForServer() {
        new Thread(() -> {
            boolean ready = portOpen();
            handler.post(() -> {
                if (ready) {
                    status.setText(R.string.ready);
                    web.loadUrl(LOCAL_URL);
                } else if (++attempts > 200) {
                    status.setText("Server did not start. Check logcat.");
                } else {
                    handler.postDelayed(this::waitForServer, 300);
                }
            });
        }, "tuya-probe").start();
    }

    private static boolean portOpen() {
        try (Socket socket = new Socket()) {
            socket.connect(new InetSocketAddress("127.0.0.1", 8000), 250);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    /** JavaScript bridge used by the QR screen to save the PNG to Gallery. */
    public class QrSaver {
        @JavascriptInterface
        public void saveQr(String dataUrl) {
            try {
                String base64 = dataUrl == null ? "" : dataUrl;
                int comma = base64.indexOf(',');
                if (comma >= 0) base64 = base64.substring(comma + 1);
                byte[] png = Base64.decode(base64, Base64.DEFAULT);

                String name = "tuya-login-qr-" + System.currentTimeMillis() + ".png";
                ContentValues values = new ContentValues();
                values.put(MediaStore.Images.Media.DISPLAY_NAME, name);
                values.put(MediaStore.Images.Media.MIME_TYPE, "image/png");
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    values.put(MediaStore.Images.Media.RELATIVE_PATH,
                               Environment.DIRECTORY_PICTURES + "/TuyaLocalKey");
                    values.put(MediaStore.Images.Media.IS_PENDING, 1);
                }
                Uri uri = getContentResolver().insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values);
                if (uri == null) throw new IllegalStateException("Gallery insert failed");
                try (OutputStream out = getContentResolver().openOutputStream(uri)) {
                    out.write(png);
                }
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    values.clear();
                    values.put(MediaStore.Images.Media.IS_PENDING, 0);
                    getContentResolver().update(uri, values, null, null);
                }
                handler.post(() -> Toast.makeText(OfflineActivity.this,
                        "QR saved to Gallery. Smart Life → Scan → Gallery se choose karein.",
                        Toast.LENGTH_LONG).show());
            } catch (Exception e) {
                Log.e(TAG, "Could not save QR image", e);
                handler.post(() -> Toast.makeText(OfflineActivity.this,
                        "QR save nahi hua: " + e.getMessage(), Toast.LENGTH_LONG).show());
            }
        }
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
