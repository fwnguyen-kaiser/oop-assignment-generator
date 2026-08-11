import java.util.ArrayList;
import java.util.List;

public class Order {
    private List<Product> products;
    private Payment payment;
    private String status;

    public Order(List<Product> products, Payment payment, String status) {
        this.products = products;
        this.payment = payment;
        this.status = status;
    }

    public List<Product> getProducts() {
        return this.products;
    }
    public void setProducts(List<Product> products) {
        this.products = products;
    }
    public Payment getPayment() {
        return this.payment;
    }
    public void setPayment(Payment payment) {
        this.payment = payment;
    }
    public String getStatus() {
        return this.status;
    }
    public void setStatus(String status) {
        this.status = status;
    }

    public void processOrder() {
    }
}