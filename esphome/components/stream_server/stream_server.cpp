#include "stream_server.h"

#include "esphome/core/helpers.h"
#include "esphome/core/log.h"
#include "esphome/core/util.h"
#include "esphome/core/version.h"

#include "esphome/components/network/util.h"
#include "esphome/components/socket/socket.h"

static const char *TAG = "stream_server";

// Orisec bridge change: a client whose network went away (a router reboot, say) never
// sends a FIN, so it stayed in the client list for good. TCP keepalive makes the stack
// probe a client that has been silent for KEEPALIVE_IDLE seconds and drop it if
// KEEPALIVE_COUNT probes go unanswered; a live client answers and is never touched.
static const int KEEPALIVE_IDLE = 30;     // seconds of silence before the first probe
static const int KEEPALIVE_INTERVAL = 5;  // seconds between probes
static const int KEEPALIVE_COUNT = 3;     // unanswered probes before the client is dropped

static void enable_keepalive(esphome::socket::Socket *socket) {
    int on = 1;
    if (socket->setsockopt(SOL_SOCKET, SO_KEEPALIVE, &on, sizeof(on)) != 0) {
        ESP_LOGW(TAG, "Couldn't enable TCP keepalive (error %d)", errno);
        return;
    }
#if defined(TCP_KEEPIDLE) && defined(TCP_KEEPINTVL) && defined(TCP_KEEPCNT)
    int idle = KEEPALIVE_IDLE, interval = KEEPALIVE_INTERVAL, count = KEEPALIVE_COUNT;
    socket->setsockopt(IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof(idle));
    socket->setsockopt(IPPROTO_TCP, TCP_KEEPINTVL, &interval, sizeof(interval));
    socket->setsockopt(IPPROTO_TCP, TCP_KEEPCNT, &count, sizeof(count));
#endif
}

// Errors that only mean "try again later"; anything else means the connection is gone.
static bool is_transient(int err) {
    return err == EWOULDBLOCK || err == EAGAIN || err == EINTR || err == ENOBUFS || err == ENOMEM;
}

using namespace esphome;

void StreamServerComponent::setup() {
    ESP_LOGCONFIG(TAG, "Setting up stream server...");

    // The make_unique() wrapper doesn't like arrays, so initialize the unique_ptr directly.
    this->buf_ = std::unique_ptr<uint8_t[]>{new uint8_t[this->buf_size_]};

    struct sockaddr_storage bind_addr;
#if ESPHOME_VERSION_CODE >= VERSION_CODE(2023, 4, 0)
    socklen_t bind_addrlen = socket::set_sockaddr_any(reinterpret_cast<struct sockaddr *>(&bind_addr), sizeof(bind_addr), this->port_);
#else
    socklen_t bind_addrlen = socket::set_sockaddr_any(reinterpret_cast<struct sockaddr *>(&bind_addr), sizeof(bind_addr), htons(this->port_));
#endif

#if ESPHOME_VERSION_CODE >= VERSION_CODE(2026, 3, 0)    
    this->socket_ = socket::socket_ip_loop_monitored(SOCK_STREAM, PF_INET).release();    
#else
    this->socket_ = socket::socket_ip(SOCK_STREAM, PF_INET);
#endif 
      
    this->socket_->setblocking(false);
    this->socket_->bind(reinterpret_cast<struct sockaddr *>(&bind_addr), bind_addrlen);
    this->socket_->listen(8);

    this->publish_sensor();
}

void StreamServerComponent::loop() {
    this->accept();
    this->read();
    this->flush();
    this->write();
    this->cleanup();
}

void StreamServerComponent::dump_config() {
    ESP_LOGCONFIG(TAG, "Stream Server:");
#if ESPHOME_VERSION_CODE >= VERSION_CODE(2025, 11, 0)
    ESP_LOGCONFIG(TAG, "  Address: %s:%u", esphome::network::get_use_address(), this->port_);
#else
    ESP_LOGCONFIG(TAG, "  Address: %s:%u", esphome::network::get_use_address().c_str(), this->port_);
#endif
#ifdef USE_BINARY_SENSOR
    LOG_BINARY_SENSOR("  ", "Connected:", this->connected_sensor_);
#endif
#ifdef USE_SENSOR
    LOG_SENSOR("  ", "Connection count:", this->connection_count_sensor_);
#endif
}

void StreamServerComponent::on_shutdown() {
#if ESPHOME_VERSION_CODE >= VERSION_CODE(2026, 3, 0)
    delete this->socket_;
    this->socket_ = nullptr;    
#endif 

    for (const Client &client : this->clients_)
        client.socket->shutdown(SHUT_RDWR);
}

void StreamServerComponent::publish_sensor() {
#ifdef USE_BINARY_SENSOR
    if (this->connected_sensor_)
        this->connected_sensor_->publish_state(this->clients_.size() > 0);
#endif
#ifdef USE_SENSOR
    if (this->connection_count_sensor_)
        this->connection_count_sensor_->publish_state(this->clients_.size());
#endif
}

void StreamServerComponent::accept() {
    struct sockaddr_storage client_addr;
    socklen_t client_addrlen = sizeof(client_addr);
    std::unique_ptr<socket::Socket> socket = this->socket_->accept(reinterpret_cast<struct sockaddr *>(&client_addr), &client_addrlen);
    if (!socket)
        return;

    socket->setblocking(false);
    enable_keepalive(socket.get());

#if ESPHOME_VERSION_CODE >= VERSION_CODE(2026, 1, 0)
    std::string identifier = std::string{esphome::socket::SOCKADDR_STR_LEN, 0};
    auto identifier_span = std::span<char, esphome::socket::SOCKADDR_STR_LEN>(identifier.data(), identifier.size());
    identifier.resize(socket->getpeername_to(identifier_span));
#else
    std::string identifier = socket->getpeername();
#endif

    this->clients_.emplace_back(std::move(socket), identifier, this->buf_head_);
    ESP_LOGD(TAG, "New client connected from %s", identifier.c_str());
    this->publish_sensor();
}

void StreamServerComponent::cleanup() {
    auto discriminator = [](const Client &client) { return !client.disconnected; };
    auto last_client = std::partition(this->clients_.begin(), this->clients_.end(), discriminator);
    if (last_client != this->clients_.end()) {
        this->clients_.erase(last_client, this->clients_.end());
        this->publish_sensor();
    }
}

void StreamServerComponent::read() {
    size_t len = 0;
    int available;
    while ((available = this->stream_->available()) > 0) {
        size_t free = this->buf_size_ - (this->buf_head_ - this->buf_tail_);
        if (free == 0) {
            // Only overwrite if nothing has been added yet, otherwise give flush() a chance to empty the buffer first.
            if (len > 0)
                return;

            ESP_LOGE(TAG, "Incoming bytes available, but outgoing buffer is full: stream will be corrupted!");
            free = std::min<size_t>(available, this->buf_size_);
            this->buf_tail_ += free;
            for (Client &client : this->clients_) {
                if (client.position < this->buf_tail_) {
                    ESP_LOGW(TAG, "Dropped %u pending bytes for client %s", this->buf_tail_ - client.position, client.identifier.c_str());
                    client.position = this->buf_tail_;
                }
            }
        }

        // Fill all available contiguous space in the ring buffer.
        len = std::min<size_t>(available, std::min<size_t>(this->buf_ahead(this->buf_head_), free));
        this->stream_->read_array(&this->buf_[this->buf_index(this->buf_head_)], len);
        this->buf_head_ += len;
    }
}

void StreamServerComponent::flush() {
    ssize_t written;
    this->buf_tail_ = this->buf_head_;
    for (Client &client : this->clients_) {
        if (client.disconnected || client.position == this->buf_head_)
            continue;

        // Split the write into two parts: from the current position to the end of the ring buffer, and from the start
        // of the ring buffer until the head. The second part might be zero if no wraparound is necessary.
        struct iovec iov[2];
        iov[0].iov_base = &this->buf_[this->buf_index(client.position)];
        iov[0].iov_len = std::min(this->buf_head_ - client.position, this->buf_ahead(client.position));
        iov[1].iov_base = &this->buf_[0];
        iov[1].iov_len = this->buf_head_ - (client.position + iov[0].iov_len);
        if ((written = client.socket->writev(iov, 2)) > 0) {
            client.position += written;
        } else if (written == 0 || !is_transient(errno)) {
            ESP_LOGD(TAG, "Client %s disconnected (write: %d)", client.identifier.c_str(), written == 0 ? 0 : errno);
            client.disconnected = true;
            continue;  // don't consider this client when calculating the tail position
        }
        // Otherwise the (TCP) transmit buffer is full: try again next loop.

        this->buf_tail_ = std::min(this->buf_tail_, client.position);
    }
}

void StreamServerComponent::write() {
    uint8_t buf[128];
    ssize_t read;
    for (Client &client : this->clients_) {
        if (client.disconnected)
            continue;

        while ((read = client.socket->read(&buf, sizeof(buf))) > 0)
            this->stream_->write_array(buf, read);

        if (read == 0 || !is_transient(errno)) {
            ESP_LOGD(TAG, "Client %s disconnected (read: %d)", client.identifier.c_str(), read == 0 ? 0 : errno);
            client.disconnected = true;
        }
        // Otherwise the (TCP) receive buffer is empty: nothing to do.
    }
}

StreamServerComponent::Client::Client(std::unique_ptr<esphome::socket::Socket> socket, std::string identifier, size_t position)
    : socket(std::move(socket)), identifier{identifier}, position{position} {}
